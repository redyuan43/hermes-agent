"""Project persisted messages into client-safe rich-media descriptors."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from hermes_constants import get_hermes_home
from gateway.message_media_archive import (
    archive_message_media_file,
    find_archived_message_media,
)
from gateway.platforms.base import BasePlatformAdapter, validate_media_delivery_path


_KNOWN_MEDIA_TOOL_NAMES = frozenset({
    "image_generate",
    "video_generate",
    "text_to_speech",
    "text_to_speech_tool",
})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"})
_AUDIO_EXTENSIONS = frozenset({
    ".ogg",
    ".opus",
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".aac",
})
_MIME_OVERRIDES = {
    ".opus": "audio/opus",
    ".m4a": "audio/mp4",
    ".mkv": "video/x-matroska",
}
_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_LEGACY_IMAGE_MARKER = re.compile(
    r"(?m)^[ \t]*\[Image attached at:[ \t]*(?P<path>[^\]\r\n]+)\][ \t]*$"
)
_LEGACY_SCREENSHOT_MARKER = re.compile(r"(?m)^[ \t]*\[screenshot\][ \t]*$")
_LEGACY_IMAGE_NAME = re.compile(
    r"^img_[0-9a-f]{12,64}\.(?:png|jpe?g|gif|webp|bmp)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MessageMediaItem:
    """One public descriptor plus its optional server-local backing file."""

    descriptor: Dict[str, Any]
    local_path: Optional[Path] = None


@dataclass(frozen=True)
class MessageMediaProjection:
    """Display-only additions for one persisted message."""

    rendered_content: Optional[str]
    media: tuple[MessageMediaItem, ...]


def project_message_media(
    messages: Iterable[Dict[str, Any]],
    *,
    session_id: str,
) -> Dict[str, MessageMediaProjection]:
    """Return media projections keyed by persisted message ID.

    Structured content stays attached to its own message. Explicit local
    ``MEDIA:`` directives are accepted only from assistant output, never from
    client-authored user rows. Known producer-tool artifacts are attached to
    the final assistant message in the same user turn. Unknown tools and
    ordinary URLs are never scanned.
    """

    snapshot = [message for message in messages if isinstance(message, dict)]
    media_by_message: Dict[str, List[MessageMediaItem]] = {}
    rendered_by_message: Dict[str, Optional[str]] = {}

    for message in snapshot:
        message_id = _message_id(message)
        if message_id is None:
            continue
        direct_media, rendered_content = _direct_message_media(
            message,
            session_id=session_id,
            message_id=message_id,
        )
        rendered_by_message[message_id] = rendered_content
        media_by_message[message_id] = direct_media

    for turn in _user_turns(snapshot):
        target = _final_assistant_message(turn)
        target_id = _message_id(target) if target is not None else None
        if target_id is None:
            continue
        media_by_message.setdefault(target_id, []).extend(
            _tool_media_for_turn(
                turn,
                session_id=session_id,
                message_id=target_id,
            )
        )

    return {
        message_id: MessageMediaProjection(
            rendered_content=rendered_by_message.get(message_id),
            media=tuple(_deduplicate(items)),
        )
        for message_id, items in media_by_message.items()
    }


def _message_id(message: Optional[Dict[str, Any]]) -> Optional[str]:
    if not message:
        return None
    value = message.get("id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _direct_message_media(
    message: Dict[str, Any],
    *,
    session_id: str,
    message_id: str,
) -> tuple[List[MessageMediaItem], Optional[str]]:
    content = message.get("content")
    items: List[MessageMediaItem] = []

    if isinstance(content, list):
        for part in content:
            candidate = _structured_media_candidate(part)
            if candidate is None:
                continue
            kind, url, name = candidate
            if url.lower().startswith("data:"):
                # Avoid duplicating a potentially multi-megabyte inline value.
                continue
            item = _remote_item(
                url,
                session_id=session_id,
                message_id=message_id,
                kind=kind,
                name=name,
                source="content_part",
            )
            if item is not None:
                items.append(item)
        return items, None

    if message.get("role") == "user" and isinstance(content, str):
        legacy_items, cleaned = _legacy_user_image_media(
            content,
            session_id=session_id,
            message_id=message_id,
        )
        if legacy_items:
            return legacy_items, cleaned

    if (
        message.get("role") != "assistant"
        or not isinstance(content, str)
        or "MEDIA:" not in content
    ):
        return items, None

    force_document = "[[as_document]]" in content
    media_files, cleaned = BasePlatformAdapter.extract_media(content)
    for raw_path, is_voice in media_files:
        item = _local_item(
            raw_path,
            session_id=session_id,
            message_id=message_id,
            source="media_directive",
            force_file=force_document,
            force_audio=is_voice,
        )
        if item is not None:
            items.append(item)
    return items, cleaned if cleaned != content else None


def _legacy_user_image_media(
    content: str,
    *,
    session_id: str,
    message_id: str,
) -> tuple[List[MessageMediaItem], Optional[str]]:
    """Recover Hermes-generated legacy image hints without trusting paths."""
    items: List[MessageMediaItem] = []
    for match in _LEGACY_IMAGE_MARKER.finditer(content):
        raw_path = match.group("path").strip()
        if not _is_managed_legacy_image(raw_path):
            continue
        item = _local_item(
            raw_path,
            session_id=session_id,
            message_id=message_id,
            source="legacy_attachment",
        )
        if item is not None:
            items.append(item)
    if not items:
        return [], None

    cleaned = _LEGACY_IMAGE_MARKER.sub("", content)
    cleaned = _LEGACY_SCREENSHOT_MARKER.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return items, cleaned


def _is_managed_legacy_image(raw_path: str) -> bool:
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or not _LEGACY_IMAGE_NAME.fullmatch(path.name):
        return False
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    home = get_hermes_home().resolve()
    roots = (
        home / "cache" / "images",
        home / "image_cache",
    )
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def _structured_media_candidate(
    part: Any,
) -> Optional[tuple[str, str, Optional[str]]]:
    if not isinstance(part, dict):
        return None
    part_type = str(part.get("type") or "").strip().lower()
    name = _nonempty_string(part.get("name") or part.get("filename"))

    if part_type in {"image_url", "input_image", "image"}:
        url = _nested_url(part.get("image_url")) or _nested_url(part.get("source"))
        url = url or _nonempty_string(part.get("url"))
        return ("image", url, name) if url else None
    if part_type in {"video_url", "input_video", "video"}:
        url = _nested_url(part.get("video_url")) or _nested_url(part.get("source"))
        url = url or _nonempty_string(part.get("url"))
        return ("video", url, name) if url else None
    if part_type in {"audio_url", "input_audio", "audio"}:
        url = _nested_url(part.get("audio_url")) or _nested_url(part.get("source"))
        url = url or _nonempty_string(part.get("url"))
        return ("audio", url, name) if url else None
    if part_type in {"file_url", "input_file", "file"}:
        url = (
            _nested_url(part.get("file_url"))
            or _nested_url(part.get("file"))
            or _nested_url(part.get("source"))
            or _nonempty_string(part.get("url"))
        )
        return ("file", url, name) if url else None
    return None


def _nested_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return _nonempty_string(value)
    if isinstance(value, dict):
        return _nonempty_string(value.get("url"))
    return None


def _user_turns(messages: List[Dict[str, Any]]) -> Iterable[List[Dict[str, Any]]]:
    current: List[Dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "user" and current:
            yield current
            current = []
        current.append(message)
    if current:
        yield current


def _final_assistant_message(
    turn: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    fallback = None
    for message in reversed(turn):
        if message.get("role") != "assistant":
            continue
        fallback = fallback or message
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return message
        if isinstance(content, list) and content:
            return message
    return fallback


def _tool_media_for_turn(
    turn: List[Dict[str, Any]],
    *,
    session_id: str,
    message_id: str,
) -> List[MessageMediaItem]:
    tool_name_by_call_id: Dict[str, str] = {}
    for message in turn:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = _nonempty_string(call.get("id") or call.get("call_id"))
            function = call.get("function")
            name = (
                _nonempty_string(function.get("name"))
                if isinstance(function, dict)
                else None
            ) or _nonempty_string(call.get("name"))
            if call_id and name:
                tool_name_by_call_id[call_id] = name

    items: List[MessageMediaItem] = []
    for message in turn:
        if message.get("role") not in {"tool", "function"}:
            continue
        call_id = _nonempty_string(
            message.get("tool_call_id") or message.get("call_id")
        )
        tool_name = _nonempty_string(
            message.get("tool_name") or message.get("name")
        ) or (tool_name_by_call_id.get(call_id) if call_id else None)
        if tool_name not in _KNOWN_MEDIA_TOOL_NAMES:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue

        if tool_name == "image_generate":
            item = _json_tool_item(
                content,
                fields=("host_image", "image", "agent_visible_image"),
                fallback_fields=("public_url",),
                session_id=session_id,
                message_id=message_id,
                kind="image",
            )
            if item is not None:
                items.append(item)
            continue

        if tool_name == "video_generate":
            item = _json_tool_item(
                content,
                fields=("host_video", "video"),
                fallback_fields=("public_url", "video"),
                session_id=session_id,
                message_id=message_id,
                kind="video",
            )
            if item is not None:
                items.append(item)
            continue

        items.extend(
            _tts_tool_items(
                content,
                session_id=session_id,
                message_id=message_id,
            )
        )
    return items


def _json_tool_item(
    content: str,
    *,
    fields: tuple[str, ...],
    fallback_fields: tuple[str, ...],
    session_id: str,
    message_id: str,
    kind: str,
) -> Optional[MessageMediaItem]:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None

    primary = next(
        (
            value
            for field in fields
            if (value := _nonempty_string(payload.get(field))) is not None
        ),
        None,
    )
    if primary is None:
        return None
    fallback = next(
        (
            value
            for field in fallback_fields
            if (value := _nonempty_string(payload.get(field))) is not None
            and value != primary
            and _is_http_url(value)
        ),
        None,
    )

    if _is_http_url(primary):
        return _remote_item(
            primary,
            session_id=session_id,
            message_id=message_id,
            kind=kind,
            name=None,
            source="tool",
        )
    return _local_item(
        primary,
        session_id=session_id,
        message_id=message_id,
        source="tool",
        fallback_url=fallback,
    )


def _tts_tool_items(
    content: str,
    *,
    session_id: str,
    message_id: str,
) -> List[MessageMediaItem]:
    candidates: List[str] = []
    remote_candidates: List[str] = []
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("success") is not False:
        for field in (
            "host_audio",
            "file_path",
            "audio",
            "path",
            "audio_url",
            "public_url",
            "url",
        ):
            value = _nonempty_string(payload.get(field))
            if not value:
                continue
            if _is_http_url(value):
                remote_candidates.append(value)
            else:
                candidates.append(value)
        media_tag = _nonempty_string(payload.get("media_tag"))
        if media_tag:
            media_files, _ = BasePlatformAdapter.extract_media(media_tag)
            candidates.extend(path for path, _is_voice in media_files)
    else:
        media_files, _ = BasePlatformAdapter.extract_media(content)
        candidates.extend(path for path, _is_voice in media_files)

    items: List[MessageMediaItem] = []
    fallback_url = remote_candidates[0] if remote_candidates else None
    for path in candidates:
        item = _local_item(
            path,
            session_id=session_id,
            message_id=message_id,
            source="tool",
            fallback_url=fallback_url,
            force_audio=True,
        )
        if item is not None:
            items.append(item)
    if not items:
        for url in remote_candidates:
            item = _remote_item(
                url,
                session_id=session_id,
                message_id=message_id,
                kind="audio",
                name=None,
                source="tool",
            )
            if item is not None:
                items.append(item)
    return items


def _local_item(
    raw_path: str,
    *,
    session_id: str,
    message_id: str,
    source: str,
    fallback_url: Optional[str] = None,
    force_file: bool = False,
    force_audio: bool = False,
) -> Optional[MessageMediaItem]:
    raw_candidate = Path(raw_path).expanduser()
    if not raw_candidate.is_absolute():
        return None
    try:
        canonical_path = raw_candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    name = _safe_name(raw_candidate.name, fallback="file")
    media_id = _media_id(
        session_id,
        message_id,
        f"file:{canonical_path}",
    )
    archived = find_archived_message_media(
        session_id=session_id,
        media_id=media_id,
        name=name,
    )
    path: Optional[Path] = archived

    safe_path = validate_media_delivery_path(raw_path, session_key=session_id)
    if path is None:
        if not safe_path:
            return None
        try:
            source_path = Path(safe_path).resolve(strict=True)
            stat = source_path.stat()
        except (OSError, RuntimeError, ValueError):
            return None
        if not source_path.is_file():
            return None
        try:
            path = archive_message_media_file(
                source_path,
                session_id=session_id,
                media_id=media_id,
                name=name,
            )
        except (OSError, RuntimeError, ValueError):
            path = source_path

    try:
        stat = path.stat()
    except OSError:
        return None

    mime_type = _mime_type(name)
    kind = "file" if force_file else _media_kind(name, mime_type)
    if force_audio:
        kind = "audio"
    descriptor: Dict[str, Any] = {
        "id": media_id,
        "kind": kind,
        "name": _safe_name(name, fallback=kind),
        "mime_type": mime_type,
        "size_bytes": stat.st_size,
        "auth_required": True,
        "source": source,
    }
    if fallback_url and _is_http_url(fallback_url):
        descriptor["fallback_url"] = fallback_url
    return MessageMediaItem(descriptor=descriptor, local_path=path)


def _remote_item(
    url: str,
    *,
    session_id: str,
    message_id: str,
    kind: str,
    name: Optional[str],
    source: str,
) -> Optional[MessageMediaItem]:
    if not _is_http_url(url):
        return None
    parsed = urlparse(url)
    resolved_name = _safe_name(
        name or Path(parsed.path).name,
        fallback=f"{kind}",
    )
    descriptor = {
        "id": _media_id(session_id, message_id, f"url:{url}"),
        "kind": kind,
        "name": resolved_name,
        "mime_type": _mime_type(resolved_name, kind=kind),
        "size_bytes": None,
        "url": url,
        "auth_required": False,
        "source": source,
    }
    return MessageMediaItem(descriptor=descriptor)


def _media_id(session_id: str, message_id: str, canonical_source: str) -> str:
    digest = hashlib.sha256(
        f"{session_id}\0{message_id}\0{canonical_source}".encode("utf-8")
    ).hexdigest()
    return f"media_{digest[:32]}"


def _media_kind(name: str, mime_type: str) -> str:
    suffix = Path(name).suffix.lower()
    if mime_type.startswith("image/") or suffix in _IMAGE_EXTENSIONS:
        return "image"
    if mime_type.startswith("video/") or suffix in _VIDEO_EXTENSIONS:
        return "video"
    if mime_type.startswith("audio/") or suffix in _AUDIO_EXTENSIONS:
        return "audio"
    return "file"


def _mime_type(name: str, *, kind: Optional[str] = None) -> str:
    suffix = Path(name).suffix.lower()
    guessed = _MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    return {
        "image": "image/*",
        "video": "video/*",
        "audio": "audio/*",
    }.get(kind, "application/octet-stream")


def _safe_name(value: str, *, fallback: str) -> str:
    name = Path(str(value or "")).name
    name = _UNSAFE_NAME_CHARS.sub("", name).strip()
    return name[:255] or fallback


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def _nonempty_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _deduplicate(items: Iterable[MessageMediaItem]) -> List[MessageMediaItem]:
    seen: set[str] = set()
    result: List[MessageMediaItem] = []
    for item in items:
        media_id = str(item.descriptor.get("id") or "")
        if not media_id or media_id in seen:
            continue
        seen.add(media_id)
        result.append(item)
    return result
