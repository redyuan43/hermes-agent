"""SIYUAN orchestration policy implemented entirely through plugin hooks."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "siyuan-orchestration"
AUXILIARY_TASK = "siyuan_route_classifier"
ROUTE_NAMES = ("luna", "terra", "sol")
DEFAULT_CLASSIFIER_PROMPT = (
    "Classify the user's request for model routing. On the first turn return "
    'a JSON object with {"base_route":"luna|terra|sol","use_moa":boolean}. '
    "On later turns the base route is already fixed; return only "
    '{"use_moa":boolean}. Escalate to MoA only for the current turn.'
)
_DELIVERY_MODES = {"notify", "notify+wake", "wake"}
_ROUTE_FIELDS = (
    "platform",
    "chat_id",
    "thread_id",
    "notifier_profile",
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _enabled(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _safe_component(value: Any) -> str:
    return "".join(
        char if char.isalnum() or char in "-_." else "_"
        for char in str(value or "unknown")
    )[:120]


def _json_object(value: Any) -> Optional[dict[str, Any]]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _route_key(route: Mapping[str, Any]) -> str:
    identity = {
        field: str(route.get(field) or "").strip()
        for field in _ROUTE_FIELDS
    }
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _board_name(value: Any = None) -> str:
    text = str(value or "").strip()
    if text:
        return text
    try:
        from hermes_cli.kanban_db import get_current_board

        return get_current_board()
    except Exception:
        return "default"


def _shared_db_path() -> Path:
    from hermes_cli.kanban_db import kanban_home

    return (
        kanban_home()
        / "plugin-data"
        / "siyuan-orchestration"
        / "state.db"
    )


def _connect_shared_db() -> sqlite3.Connection:
    from hermes_cli.sqlite_safe_read import connect_tracked
    from hermes_state import apply_wal_with_fallback

    path = _shared_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    conn = connect_tracked(
        path,
        connect_fn=sqlite3.connect,
        isolation_level=None,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(
        conn,
        db_label="siyuan-orchestration/state.db",
    )
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fallback_routes (
            board TEXT NOT NULL,
            task_id TEXT NOT NULL,
            primary_route_key TEXT NOT NULL,
            origin_route_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (board, task_id, primary_route_key)
        );
        """
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


@contextlib.contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextlib.contextmanager
def _profile_scope(profile_name: str) -> Iterator[None]:
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    profile_home = get_profile_dir(profile_name)
    home_token = set_hermes_home_override(profile_home)
    secret_token = set_secret_scope(build_profile_secret_scope(Path(profile_home)))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


class SiyuanOrchestrationPlugin:
    """Stateful hook implementation bound to one ``PluginContext``."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def _config(self, key: str, default: Any = None) -> Any:
        return self.ctx.get_config(key, default)

    def _routing_config(self) -> dict[str, Any]:
        raw = _mapping(self._config("model_routing", {}))
        profiles = _mapping(raw.get("profiles"))
        normalized_profiles: dict[str, dict[str, str]] = {}
        for name in ROUTE_NAMES:
            route = _mapping(profiles.get(name) or raw.get(name))
            provider = str(route.get("provider") or "").strip()
            model = str(route.get("model") or "").strip()
            if provider and model:
                normalized_profiles[name] = {
                    "provider": provider,
                    "model": model,
                }
        trace = _mapping(raw.get("trace"))
        return {
            "enabled": _enabled(raw.get("enabled"), False),
            "profiles": normalized_profiles,
            "moa": raw.get("moa"),
            "classifier_prompt": str(
                _mapping(raw.get("classifier")).get("prompt")
                or raw.get("classifier_prompt")
                or DEFAULT_CLASSIFIER_PROMPT
            ),
            "trace": {
                "enabled": _enabled(trace.get("enabled"), True),
                "retention_days": _positive_int(
                    trace.get("retention_days"), 7
                ),
            },
        }

    def _state_key(self, conversation_id: str) -> str:
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return f"route:{digest}"

    def _read_base_route(self, conversation_id: str) -> Optional[str]:
        if not conversation_id:
            return None
        try:
            state = self.ctx.state.get(self._state_key(conversation_id), {})
        except Exception:
            logger.debug("Could not read SIYUAN route state", exc_info=True)
            return None
        if not isinstance(state, Mapping):
            return None
        route = str(state.get("base_route") or "").strip().lower()
        return route if route in ROUTE_NAMES else None

    def _write_base_route(self, conversation_id: str, route: str) -> None:
        if not conversation_id:
            return
        try:
            self.ctx.state.data_dir.mkdir(parents=True, exist_ok=True)
            self.ctx.state.data_dir.chmod(0o700)
            self.ctx.state.set(
                self._state_key(conversation_id),
                {
                    "base_route": route,
                    "created_at": int(time.time()),
                },
            )
        except Exception:
            logger.warning(
                "Could not persist SIYUAN base model route", exc_info=True
            )

    def _trace(
        self,
        conversation_id: str,
        *,
        cfg: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> None:
        trace_cfg = _mapping(cfg.get("trace"))
        if not trace_cfg.get("enabled"):
            return
        try:
            base = self.ctx.state.data_dir / "routing-traces"
            base.mkdir(parents=True, exist_ok=True)
            base.chmod(0o700)
            trace_id = hashlib.sha256(
                conversation_id.encode("utf-8")
            ).hexdigest()[:24]
            path = base / f"routing-{_safe_component(trace_id)}.jsonl"
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"ts": time.time(), **dict(record)},
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
            cutoff = time.time() - (
                _positive_int(trace_cfg.get("retention_days"), 7) * 86400
            )
            for candidate in base.glob("routing-*.jsonl"):
                try:
                    if candidate.stat().st_mtime < cutoff:
                        candidate.unlink()
                except OSError:
                    continue
        except Exception:
            logger.debug("Could not write SIYUAN routing trace", exc_info=True)

    def _classify(
        self,
        message: str,
        *,
        initial: bool,
        prompt: str,
    ) -> tuple[dict[str, Any], str, Optional[str]]:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "use_moa": {"type": "boolean"},
            },
            "required": ["use_moa"],
            "additionalProperties": False,
        }
        if initial:
            schema["properties"]["base_route"] = {
                "type": "string",
                "enum": list(ROUTE_NAMES),
            }
            schema["required"] = ["base_route", "use_moa"]
        try:
            response = self.ctx.llm.complete_structured(
                instructions=prompt,
                input=[{"type": "text", "text": message}],
                json_schema=schema,
                json_mode=True,
                schema_name="siyuan_model_route",
                temperature=0,
                max_tokens=64,
                timeout=20,
                purpose="SIYUAN gateway model routing",
                task=AUXILIARY_TASK,
            )
            parsed = (
                dict(response.parsed)
                if isinstance(response.parsed, Mapping)
                else None
            )
            if parsed is None and str(getattr(response, "text", "")).strip().lower() == "moa":
                parsed = (
                    {"base_route": "terra", "use_moa": True}
                    if initial
                    else {"use_moa": True}
                )
            if parsed is None or not isinstance(parsed.get("use_moa"), bool):
                raise ValueError("classifier did not return valid routing JSON")
            if initial:
                base_route = str(parsed.get("base_route") or "").lower()
                if base_route not in ROUTE_NAMES:
                    raise ValueError("classifier returned an invalid base route")
                parsed["base_route"] = base_route
            return parsed, str(getattr(response, "text", "") or ""), None
        except Exception as exc:
            fallback = (
                {"base_route": "terra", "use_moa": False}
                if initial
                else {"use_moa": False}
            )
            return fallback, "", str(exc)

    @staticmethod
    def _moa_route(raw: Any) -> dict[str, str]:
        if isinstance(raw, str):
            return {"provider": "moa", "model": raw.strip() or "default"}
        value = _mapping(raw)
        return {
            "provider": "moa",
            "model": str(
                value.get("model") or value.get("preset") or "default"
            ).strip(),
        }

    def transform_gateway_model_route(
        self,
        *,
        message: str = "",
        conversation_id: str = "",
        current: Any = None,
        **_: Any,
    ) -> Optional[dict[str, str]]:
        cfg = self._routing_config()
        if not cfg["enabled"] or not conversation_id:
            return None
        started = time.perf_counter()
        base_route = self._read_base_route(conversation_id)
        initial = base_route is None
        decision, raw_output, error = self._classify(
            str(message or ""),
            initial=initial,
            prompt=cfg["classifier_prompt"],
        )
        if initial:
            base_route = str(decision.get("base_route") or "terra")
            if base_route not in ROUTE_NAMES:
                base_route = "terra"
            self._write_base_route(conversation_id, base_route)
        use_moa = bool(decision.get("use_moa"))
        if use_moa:
            selected = self._moa_route(cfg.get("moa"))
            reason = (
                f"{base_route}+moa" if initial else f"{base_route}:one-shot-moa"
            )
        else:
            selected = _mapping(cfg["profiles"].get(base_route))
            reason = base_route if initial else f"{base_route}:pinned"
        route = None
        provider = str(selected.get("provider") or "").strip()
        model = str(selected.get("model") or "").strip()
        if provider and model:
            route = {
                "action": "route",
                "provider": provider,
                "model": model,
                "reason": reason,
            }
        self._trace(
            conversation_id,
            cfg=cfg,
            record={
                "initial": initial,
                "base_route": base_route,
                "use_moa": use_moa,
                "selected": {
                    "provider": provider,
                    "model": model,
                },
                "primary": _mapping(current),
                "classifier_output": raw_output,
                "error": error,
                "duration_ms": round(
                    (time.perf_counter() - started) * 1000, 2
                ),
            },
        )
        return route

    def pre_tool_call(
        self,
        *,
        tool_name: str = "",
        args: Any = None,
        **_: Any,
    ) -> Optional[dict[str, str]]:
        if tool_name != "kanban_create":
            return None
        raw = self._config("allowed_assignees", [])
        if isinstance(raw, str):
            values = raw.split(",")
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            values = []
        allowed = {
            str(value).strip() for value in values if str(value).strip()
        }
        if not allowed:
            return None
        assignee = str(_mapping(args).get("assignee") or "").strip()
        if assignee in allowed:
            return None
        return {
            "action": "block",
            "message": (
                f"assignee {assignee!r} is not allowed by "
                f"{PLUGIN_ID}; allowed profiles: {', '.join(sorted(allowed))}"
            ),
        }

    def _delivery_mode(self) -> str:
        mode = str(
            _mapping(self._config("wake", {})).get("mode") or "notify"
        ).strip()
        return mode if mode in _DELIVERY_MODES else "notify"

    def _resolve_delivery(
        self,
        *,
        policy_profile: str,
    ) -> Optional[dict[str, Any]]:
        raw = self._config("completion_delivery", None)
        if not raw:
            return None
        delivery = _mapping(raw)
        sender_profile = str(delivery.get("sender_profile") or "").strip()
        platform = str(delivery.get("platform") or "").strip().lower()
        chat_id = str(delivery.get("chat_id") or "").strip()
        use_home = delivery.get("use_home_channel") is True
        if not sender_profile or not platform or bool(chat_id) == use_home:
            logger.warning("Invalid SIYUAN completion_delivery settings")
            return None
        thread_id = str(delivery.get("thread_id") or "").strip()
        try:
            from gateway.config import Platform
            from hermes_cli.profiles import profile_exists

            platform_enum = Platform(platform)
            if not profile_exists(sender_profile):
                raise ValueError(
                    f"sender profile {sender_profile!r} does not exist"
                )
        except Exception:
            logger.warning(
                "Invalid SIYUAN completion delivery target",
                exc_info=True,
            )
            return None
        if use_home:
            try:
                from gateway.config import load_gateway_config

                with _profile_scope(sender_profile):
                    home = load_gateway_config().get_home_channel(platform_enum)
                if home is None or not str(home.chat_id).strip():
                    raise ValueError("home channel is not configured")
                chat_id = str(home.chat_id).strip()
                if not thread_id:
                    thread_id = str(home.thread_id or "").strip()
            except Exception:
                logger.warning(
                    "Could not resolve SIYUAN completion home channel",
                    exc_info=True,
                )
                return None
        return {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "notifier_profile": sender_profile,
            "delivery_mode": self._delivery_mode(),
            "delivery_metadata": {
                "policy_plugin": PLUGIN_ID,
                "policy_profile": policy_profile or "default",
            },
        }

    def _save_fallback(
        self,
        *,
        board: str,
        task_id: str,
        primary: Mapping[str, Any],
        origin: Mapping[str, Any],
    ) -> None:
        conn = _connect_shared_db()
        try:
            with _immediate(conn):
                conn.execute(
                    """
                    INSERT INTO fallback_routes
                        (board, task_id, primary_route_key, origin_route_json,
                         created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(board, task_id, primary_route_key) DO UPDATE SET
                        origin_route_json = excluded.origin_route_json,
                        created_at = excluded.created_at
                    """,
                    (
                        board,
                        task_id,
                        _route_key(primary),
                        json.dumps(dict(origin), sort_keys=True),
                        int(time.time()),
                    ),
                )
        finally:
            conn.close()

    def transform_kanban_create_subscription(
        self,
        *,
        profile_name: str = "",
        **_: Any,
    ) -> Optional[dict[str, Any]]:
        delivery = self._resolve_delivery(policy_profile=profile_name)
        return {"route": delivery} if delivery is not None else None

    def post_kanban_create_subscription(
        self,
        *,
        task_id: str = "",
        board: str = "",
        origin: Any = None,
        subscription: Any = None,
        **_: Any,
    ) -> None:
        primary = _mapping(subscription)
        fallback = _mapping(origin)
        metadata = _mapping(primary.get("delivery_metadata"))
        if (
            not task_id
            or metadata.get("policy_plugin") != PLUGIN_ID
            or not fallback
            or _route_key(primary) == _route_key(fallback)
        ):
            return
        fallback["delivery_mode"] = primary.get("delivery_mode") or "notify"
        try:
            self._save_fallback(
                board=_board_name(board),
                task_id=str(task_id),
                primary=primary,
                origin=fallback,
            )
        except Exception:
            logger.warning(
                "Could not persist SIYUAN Kanban fallback route",
                exc_info=True,
            )

    def transform_kanban_delivery_failure(
        self,
        *,
        task_id: str = "",
        board: str = "",
        subscription: Any = None,
        failures: int = 0,
        **_: Any,
    ) -> Optional[dict[str, Any]]:
        fallback_cfg = _mapping(self._config("fallback", {}))
        if not _enabled(fallback_cfg.get("enabled"), True):
            return None
        threshold = _positive_int(
            fallback_cfg.get(
                "after_attempts", fallback_cfg.get("failure_threshold")
            ),
            3,
        )
        if int(failures or 0) < threshold:
            return None
        route = _mapping(subscription)
        if not task_id or not route:
            return None
        conn = _connect_shared_db()
        try:
            with _immediate(conn):
                row = conn.execute(
                    """
                    SELECT origin_route_json
                      FROM fallback_routes
                     WHERE board = ? AND task_id = ? AND primary_route_key = ?
                    """,
                    (
                        _board_name(board),
                        str(task_id),
                        _route_key(route),
                    ),
                ).fetchone()
                if row is None:
                    return None
                origin = _json_object(row["origin_route_json"])
                if not origin:
                    return None
                return {"route": origin}
        finally:
            conn.close()

def register(ctx: Any) -> None:
    plugin = SiyuanOrchestrationPlugin(ctx)
    ctx.register_auxiliary_task(
        key=AUXILIARY_TASK,
        display_name="SIYUAN route classifier",
        description="Classify Luna, Terra, Sol, and one-turn MoA routes.",
        defaults={"provider": "auto", "model": "", "timeout": 20},
    )
    ctx.register_hook(
        "transform_gateway_model_route",
        plugin.transform_gateway_model_route,
    )
    ctx.register_hook("pre_tool_call", plugin.pre_tool_call)
    ctx.register_hook(
        "transform_kanban_create_subscription",
        plugin.transform_kanban_create_subscription,
    )
    ctx.register_hook(
        "post_kanban_create_subscription",
        plugin.post_kanban_create_subscription,
    )
    ctx.register_hook(
        "transform_kanban_delivery_failure",
        plugin.transform_kanban_delivery_failure,
    )
