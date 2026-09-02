"""Authenticated localhost HTTP ingress for short voice turns."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from hermes_cli.config import get_hermes_home

from .audio import (
    SUPPORTED_CONTENT_TYPES,
    AudioPreparationError,
    prepare_audio,
)
from .cone_codec import opus_available

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 18_781
_MAX_BODY = 3 * 1024 * 1024
_MAX_CONNECTIONS = 16
_MAX_TRACKED_REQUESTS = 4_096
_REQUEST_TIMEOUT_SECONDS = 15.0
_REQUEST_DEADLINE_SECONDS = 30.0
_WEIXIN_RETRY_SCAN_SECONDS = 2.0
_WEIXIN_ACTION_REQUIRED = "send_weixin_message"
_SOURCE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_POST_PATHS = {
    "/v1/utterances",
    "/voice-ingress/v1/utterances",
    "/cone-voice/v1/utterances",
}
_HEALTH_PATHS = {"/health", "/voice-ingress/health", "/cone-voice/health"}
_STATUS_PREFIXES = tuple(f"{path}/" for path in sorted(_POST_PATHS))
_STATUS_ORDER = {
    "accepted": 0,
    "transcribing": 1,
    "processing": 2,
    "waiting_for_weixin": 3,
    "completed": 4,
    "failed": 4,
    "cancelled": 4,
    "interrupted": 4,
}
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


def _int_setting(raw: Any, default: int) -> int:
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _float_setting(
    raw: Any,
    default: float,
    *,
    minimum: float,
) -> float:
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


class _VoiceIngressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, adapter: "VoiceIngressAdapter"):
        self._request_slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        super().__init__(address, handler)
        self.adapter = adapter

    def process_request(self, request, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class VoiceIngressAuthorizationError(RuntimeError):
    pass


class VoiceIngressRequestHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        self.request.settimeout(_REQUEST_TIMEOUT_SECONDS)
        super().setup()
        self._deadline_timer = threading.Timer(
            _REQUEST_DEADLINE_SECONDS,
            self._expire_request,
        )
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def finish(self) -> None:
        timer = getattr(self, "_deadline_timer", None)
        if timer is not None:
            timer.cancel()
        super().finish()

    def _expire_request(self) -> None:
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def adapter(self) -> "VoiceIngressAdapter":
        return self.server.adapter  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002,N802
        logger.debug("Voice ingress http: " + format, *args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in _HEALTH_PATHS:
            self._json(
                200,
                {
                    "status": "ok",
                    "weixin_ready": self.adapter.weixin_ready(),
                    "cone_opus_ready": opus_available(),
                },
            )
            return
        request_id = next(
            (
                path[len(prefix) :]
                for prefix in _STATUS_PREFIXES
                if path.startswith(prefix)
            ),
            "",
        )
        if not request_id or "/" in request_id:
            self._json(404, {"error": "not_found"})
            return
        if not self.adapter.authorize(self.headers.get("Authorization", "")):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            parsed_request_id = uuid.UUID(request_id)
        except (ValueError, AttributeError):
            self._json(400, {"error": "invalid_request_id"})
            return
        if str(parsed_request_id) != request_id:
            self._json(400, {"error": "invalid_request_id"})
            return
        status = self.adapter.request_status(request_id)
        if status is None:
            self._json(404, {"error": "not_found"})
            return
        self._json(200, status)

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] not in _POST_PATHS:
            self._json(404, {"error": "not_found"})
            return
        if not self.adapter.authorize(self.headers.get("Authorization", "")):
            self._json(401, {"error": "unauthorized"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type not in SUPPORTED_CONTENT_TYPES:
            self._json(415, {"error": "unsupported_content_type"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "content_length_required"})
            return
        if content_length < 1 or content_length > _MAX_BODY:
            self._json(413, {"error": "payload_too_large"})
            return

        request_id = (
            self.headers.get("X-Voice-Request-Id")
            or self.headers.get("X-Cone-Request-Id", "")
        ).strip()
        source_id = (
            self.headers.get("X-Voice-Source-Id")
            or self.headers.get("X-Cone-Device-Id", "")
        ).strip()
        claimed_digest = self.headers.get("X-Content-SHA256", "").strip().lower()
        try:
            parsed_request_id = uuid.UUID(request_id)
        except (ValueError, AttributeError):
            self._json(400, {"error": "invalid_request_id"})
            return
        if str(parsed_request_id) != request_id:
            self._json(400, {"error": "invalid_request_id"})
            return
        if not _SOURCE_ID.fullmatch(source_id):
            self._json(400, {"error": "invalid_source_id"})
            return

        try:
            payload = self.rfile.read(content_length)
        except TimeoutError:
            self._json(408, {"error": "request_timeout"})
            return
        if len(payload) != content_length:
            self._json(400, {"error": "truncated_payload"})
            return
        digest = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(digest, claimed_digest):
            self._json(400, {"error": "digest_mismatch"})
            return
        try:
            duplicate = self.adapter.accept(
                request_id,
                source_id,
                digest,
                content_type,
                payload,
            )
        except AudioPreparationError as exc:
            self._json(422, {"error": "invalid_audio", "detail": str(exc)[:256]})
            return
        except VoiceIngressAuthorizationError as exc:
            self._json(
                403, {"error": "target_not_authorized", "detail": str(exc)[:256]}
            )
            return
        except TimeoutError:
            self._json(503, {"error": "gateway_timeout"})
            return
        except RuntimeError as exc:
            self._json(503, {"error": "gateway_unavailable", "detail": str(exc)[:256]})
            return
        except Exception:
            logger.exception("Voice ingress request failed request=%s", request_id)
            self._json(500, {"error": "internal_error"})
            return
        self._json(
            202,
            {
                "accepted": True,
                "request_id": request_id,
                "duplicate": duplicate,
                "delivery": "at_most_once",
                "status_url": f"{self.path.split('?', 1)[0].rstrip('/')}/{request_id}",
            },
        )


class VoiceIngressAdapter(BasePlatformAdapter):
    """Own the ingress server and marshal accepted audio onto the gateway loop."""

    supports_async_delivery = False

    def __init__(self, config):
        super().__init__(config=config, platform=Platform("voice_ingress"))
        extra = getattr(config, "extra", {}) or {}
        self.host = "127.0.0.1"
        self.port = _int_setting(extra.get("port"), _DEFAULT_PORT)
        self.token = str(extra.get("bearer_token") or "").strip()
        self.target_user_id = str(extra.get("target_user_id") or "").strip()
        self.target_chat_id = (
            str(extra.get("target_chat_id") or "").strip() or self.target_user_id
        )
        self.cache_dir = Path(get_hermes_home()) / "cache" / "voice_ingress"
        self.retention_hours = _float_setting(
            extra.get("cache_hours"),
            24.0,
            minimum=1.0,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._httpd: Optional[_VoiceIngressServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._weixin_retry_task: Optional[asyncio.Task] = None
        self._requests: dict[str, tuple[str, bool]] = {}
        self._requests_changed = threading.Condition()

    @property
    def name(self) -> str:
        return "Voice Ingress"

    @property
    def authorization_is_upstream(self) -> bool:
        return True

    async def connect(self, **_kwargs) -> bool:
        if not self.token or not self.target_user_id:
            self._set_fatal_error(
                "missing_config",
                "Voice ingress token and Weixin target user are required",
                retryable=False,
            )
            return False
        self._loop = asyncio.get_running_loop()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.chmod(0o700)
        self._cleanup_cache()
        self._mark_interrupted_requests()
        try:
            self._httpd = _VoiceIngressServer(
                (self.host, self.port),
                VoiceIngressRequestHandler,
                self,
            )
        except OSError as exc:
            self._set_fatal_error(
                "bind_failed",
                f"Voice ingress bind failed: {exc}",
                retryable=True,
            )
            return False
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="voice-ingress-http",
            daemon=True,
        )
        self._server_thread.start()
        self._weixin_retry_task = asyncio.create_task(
            self._weixin_retry_loop(),
            name="voice-ingress-weixin-retry",
        )
        self._mark_connected()
        logger.info(
            "Voice ingress listening on http://%s:%s", self.host, self.bound_port
        )
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._weixin_retry_task is not None:
            self._weixin_retry_task.cancel()
            try:
                await self._weixin_retry_task
            except asyncio.CancelledError:
                pass
            self._weixin_retry_task = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._loop = None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return SendResult(success=False, error="Voice ingress is ingress-only")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Voice ingress", "type": "dm"}

    @property
    def bound_port(self) -> int:
        return (
            int(self._httpd.server_address[1]) if self._httpd is not None else self.port
        )

    def authorize(self, header: str) -> bool:
        scheme, _, candidate = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            candidate.strip(),
            self.token,
        )

    def weixin_ready(self) -> bool:
        adapter = self._weixin_adapter()
        if adapter is None or getattr(adapter, "_message_handler", None) is None:
            return False
        ready = getattr(adapter, "session_ready", None)
        if ready is None:
            ready = getattr(adapter, "is_connected", False)
        return bool(ready() if callable(ready) else ready)

    def accept(
        self,
        request_id: str,
        source_id: str,
        digest: str,
        content_type: str,
        payload: bytes,
    ) -> bool:
        marker = self.cache_dir / f"{request_id}.json"
        request_fingerprint = hashlib.sha256(
            f"{content_type}\0{digest}".encode("utf-8")
        ).hexdigest()
        wait_deadline = time.monotonic() + 12
        with self._requests_changed:
            while True:
                prior = self._requests.get(request_id)
                if prior is None:
                    break
                prior_fingerprint, accepted = prior
                if prior_fingerprint != request_fingerprint:
                    raise RuntimeError("request id already used with different content")
                if accepted:
                    return True
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("matching request is still processing")
                self._requests_changed.wait(remaining)

            if marker.is_file():
                try:
                    saved = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    saved = {}
                if (
                    saved.get("sha256") == digest
                    and saved.get("content_type") == content_type
                    and saved.get("status") in _STATUS_ORDER
                ):
                    self._remember_accepted_locked(request_id, request_fingerprint)
                    return True
                if saved and (
                    saved.get("sha256") != digest
                    or saved.get("content_type") != content_type
                ):
                    raise RuntimeError(
                        "request id already persisted with different content"
                    )
                marker.unlink(missing_ok=True)
                (self.cache_dir / f"{request_id}.wav").unlink(missing_ok=True)
            self._requests[request_id] = (request_fingerprint, False)

        wav_path = self.cache_dir / f"{request_id}.wav"
        temp_path = self.cache_dir / f".{request_id}.wav.tmp"
        marker_temp = self.cache_dir / f".{request_id}.json.tmp"
        try:
            prepared = prepare_audio(payload, content_type, temp_path)
            os.replace(temp_path, wav_path)
            wav_path.chmod(0o600)
            marker_temp.write_text(
                json.dumps(
                    {
                        "request_id": request_id,
                        "source_id": source_id,
                        "sha256": digest,
                        "content_type": content_type,
                        "source_format": prepared.source_format,
                        "duration_seconds": round(prepared.duration_seconds, 3),
                        "accepted_at": int(time.time()),
                        "updated_at": int(time.time()),
                        "status": "accepted",
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            marker_temp.chmod(0o600)
            os.replace(marker_temp, marker)
            loop = self._loop
            if loop is None or not loop.is_running():
                raise RuntimeError("SIYUAN gateway loop is unavailable")
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch(request_id, source_id, wav_path),
                loop,
            )
            try:
                future.result(timeout=10)
            except FuturesTimeout as exc:
                future.cancel()
                raise TimeoutError("SIYUAN gateway event loop timed out") from exc
            with self._requests_changed:
                self._remember_accepted_locked(request_id, request_fingerprint)
                self._requests_changed.notify_all()
            self._cleanup_cache()
            return False
        except Exception:
            with self._requests_changed:
                self._requests.pop(request_id, None)
                self._requests_changed.notify_all()
            temp_path.unlink(missing_ok=True)
            marker_temp.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
            raise

    async def _dispatch(self, request_id: str, source_id: str, wav_path: Path) -> None:
        weixin = self._weixin_adapter()
        if not self.weixin_ready() or weixin is None:
            raise RuntimeError("Weixin adapter is not connected")
        source = weixin.build_source(
            chat_id=self.target_chat_id,
            chat_type="dm",
            user_id=self.target_user_id,
            user_name="Voice Ingress",
        )
        runner = getattr(self, "gateway_runner", None)
        authorize = getattr(runner, "_is_user_authorized", None)
        if not callable(authorize) or not authorize(source):
            raise VoiceIngressAuthorizationError(
                "configured Weixin target is not authorized by SIYUAN",
            )
        event = MessageEvent(
            text="",
            message_type=MessageType.VOICE,
            source=source,
            raw_message={
                "source": "voice_ingress",
                "source_id": source_id,
                "request_id": request_id,
            },
            message_id=f"voice-{request_id}",
            media_urls=[str(wav_path)],
            media_types=["audio/wav"],
            internal=False,
            metadata={
                "voice_ingress": True,
                "voice_ingress_request_id": request_id,
                "source_id": source_id,
                "combined_reply": True,
            },
            processing_status_callback=lambda stage, details: (
                self._record_processing_status(
                    request_id,
                    stage,
                    details,
                )
            ),
        )
        await weixin.handle_message(event)

    async def _record_processing_status(
        self,
        request_id: str,
        stage: str,
        details: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(
            self.update_request_status,
            request_id,
            stage,
            details,
        )

    def request_status(self, request_id: str) -> Optional[dict[str, Any]]:
        marker = self.cache_dir / f"{request_id}.json"
        with self._requests_changed:
            try:
                saved = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
        status = str(saved.get("status") or "")
        if status not in _STATUS_ORDER:
            return None
        result: dict[str, Any] = {
            "request_id": request_id,
            "status": status,
            "accepted_at": int(saved.get("accepted_at") or 0),
            "updated_at": int(saved.get("updated_at") or saved.get("accepted_at") or 0),
            "terminal": status in _TERMINAL_STATUSES,
        }
        transcript = saved.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            result["transcript"] = transcript.strip()
        error = saved.get("error")
        if isinstance(error, str) and error.strip():
            result["error"] = error.strip()
        if status == "waiting_for_weixin":
            result["action_required"] = _WEIXIN_ACTION_REQUIRED
        return result

    def update_request_status(
        self,
        request_id: str,
        stage: str,
        details: Optional[dict[str, Any]] = None,
    ) -> bool:
        if stage not in _STATUS_ORDER:
            return False
        marker = self.cache_dir / f"{request_id}.json"
        payload = dict(details or {})
        with self._requests_changed:
            try:
                saved = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            current = str(saved.get("status") or "accepted")
            if current in _TERMINAL_STATUSES:
                return False

            pending_reply = payload.get("pending_reply")
            context_required = (
                stage == "failed"
                and payload.get("delivery_failure_reason") == "context_token_required"
                and isinstance(pending_reply, str)
                and bool(pending_reply.strip())
            )
            if context_required:
                stage = "waiting_for_weixin"
            if _STATUS_ORDER.get(stage, -1) < _STATUS_ORDER.get(current, -1):
                return False

            transcript = payload.get("transcript")
            if isinstance(transcript, str) and transcript.strip():
                saved["transcript"] = transcript.strip()
            if context_required:
                weixin = self._weixin_adapter()
                snapshot = getattr(weixin, "context_token_snapshot", None)
                _, token_updated_at = (
                    snapshot(self.target_user_id)
                    if callable(snapshot)
                    else (None, 0.0)
                )
                saved["pending_reply"] = pending_reply.strip()
                saved["required_token_updated_at"] = float(token_updated_at)
                saved["waiting_since"] = time.time()
                saved["retry_count"] = 0
                saved["error"] = "请先给 SIYUAN 发一条微信消息，系统会自动补发"
            elif stage in {"failed", "cancelled", "interrupted"}:
                supplied_error = payload.get("error")
                saved["error"] = (
                    supplied_error.strip()
                    if isinstance(supplied_error, str) and supplied_error.strip()
                    else {
                        "failed": "SIYUAN 回复未能送达",
                        "cancelled": "处理已取消",
                        "interrupted": "服务重启，处理已中断",
                    }[stage]
                )
            if stage in _TERMINAL_STATUSES:
                for key in (
                    "pending_reply",
                    "required_token_updated_at",
                    "waiting_since",
                    "retry_started_at",
                ):
                    saved.pop(key, None)
            saved["status"] = stage
            saved["updated_at"] = int(time.time())
            return self._write_status_marker(marker, saved, "status")

    def _write_status_marker(
        self,
        marker: Path,
        saved: dict[str, Any],
        suffix: str,
    ) -> bool:
        temp = self.cache_dir / f".{marker.stem}.{suffix}.tmp"
        try:
            temp.write_text(
                json.dumps(saved, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temp.chmod(0o600)
            os.replace(temp, marker)
            return True
        except OSError:
            temp.unlink(missing_ok=True)
            logger.warning(
                "Voice ingress marker update failed request=%s",
                marker.stem,
                exc_info=True,
            )
            return False

    def _mark_interrupted_requests(self) -> None:
        for marker in self.cache_dir.glob("*.json"):
            try:
                saved = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            request_id = str(saved.get("request_id") or marker.stem)
            status = str(saved.get("status") or "")
            if status in {
                "accepted",
                "transcribing",
                "processing",
            }:
                self.update_request_status(request_id, "interrupted")
            elif status == "waiting_for_weixin" and int(saved.get("retry_count") or 0) > 0:
                self.update_request_status(
                    request_id,
                    "failed",
                    {"error": "自动补发期间服务重启，投递状态无法确认"},
                )

    async def _weixin_retry_loop(self) -> None:
        while True:
            try:
                await self._retry_waiting_requests()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Voice ingress Weixin retry scan failed", exc_info=True)
            await asyncio.sleep(_WEIXIN_RETRY_SCAN_SECONDS)

    async def _retry_waiting_requests(self) -> None:
        weixin = self._weixin_adapter()
        snapshot = getattr(weixin, "context_token_snapshot", None)
        if weixin is None or not callable(snapshot):
            return
        token, token_updated_at = snapshot(self.target_user_id)
        now = time.time()
        for marker in sorted(self.cache_dir.glob("*.json")):
            request_id = marker.stem
            pending_reply = ""
            expired = False
            with self._requests_changed:
                try:
                    saved = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if str(saved.get("status") or "") != "waiting_for_weixin":
                    continue
                waiting_since = float(saved.get("waiting_since") or 0.0)
                expired = now - waiting_since >= self.retention_hours * 3600
                if not expired:
                    required_at = float(saved.get("required_token_updated_at") or 0.0)
                    if (
                        not token
                        or token_updated_at <= required_at
                        or int(saved.get("retry_count") or 0) > 0
                    ):
                        continue
                    pending_reply = str(saved.get("pending_reply") or "").strip()
                    if not pending_reply:
                        expired = True
                    else:
                        saved["retry_count"] = 1
                        saved["retry_started_at"] = now
                        saved["updated_at"] = int(now)
                        if not self._write_status_marker(marker, saved, "retry"):
                            continue
            if expired:
                self.update_request_status(
                    request_id,
                    "failed",
                    {"error": "等待微信消息超时，请重新发送语音"},
                )
                continue

            result = await weixin.send(self.target_chat_id, pending_reply)
            if result.success:
                self.update_request_status(request_id, "completed")
                logger.info("Voice ingress Weixin reply recovered request=%s", request_id)
            else:
                self.update_request_status(
                    request_id,
                    "failed",
                    {"error": "获得新微信会话后自动补发仍然失败"},
                )
                logger.warning(
                    "Voice ingress Weixin retry failed request=%s error=%s",
                    request_id,
                    result.error,
                )

    def _weixin_adapter(self):
        runner = getattr(self, "gateway_runner", None)
        if runner is None:
            return None
        adapter = getattr(runner, "adapters", {}).get(Platform.WEIXIN)
        if adapter is not None:
            return adapter
        for adapters in (getattr(runner, "_profile_adapters", None) or {}).values():
            if isinstance(adapters, dict) and Platform.WEIXIN in adapters:
                return adapters[Platform.WEIXIN]
        return None

    def _remember_accepted_locked(self, request_id: str, fingerprint: str) -> None:
        self._requests[request_id] = (fingerprint, True)
        while len(self._requests) > _MAX_TRACKED_REQUESTS:
            removable = next(
                (key for key, (_, accepted) in self._requests.items() if accepted),
                None,
            )
            if removable is None:
                break
            self._requests.pop(removable, None)

    def _cleanup_cache(self) -> None:
        cutoff = time.time() - self.retention_hours * 3600
        try:
            paths = list(self.cache_dir.iterdir())
        except OSError:
            return
        for path in paths:
            try:
                if not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
                marker = path if path.suffix == ".json" else path.with_suffix(".json")
                if marker.is_file():
                    try:
                        saved = json.loads(marker.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        saved = {}
                    if str(saved.get("status") or "") == "waiting_for_weixin":
                        continue
                path.unlink()
            except OSError:
                logger.debug("Could not remove stale voice-ingress cache file %s", path)
        with self._requests_changed:
            for request_id, (_, accepted) in tuple(self._requests.items()):
                if accepted and not (self.cache_dir / f"{request_id}.json").is_file():
                    self._requests.pop(request_id, None)
