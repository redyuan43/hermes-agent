"""Configuration-driven gateway model routing.

Classifier calls are side-channel requests. They never enter the conversation
transcript, so the acting agent's prompt cache remains stable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

ROUTE_NAMES = ("luna", "terra", "sol")
DEFAULT_CLASSIFIER_PROMPT = (
    "Classify the user's request for model routing. Reply with exactly one "
    'JSON object: {"base_route":"luna|terra|sol","use_moa":true|false}. '
    "On later turns, base_route is ignored and only use_moa is considered."
)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bool(value: Any, default: bool = False) -> bool:
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


def normalize_model_routing_config(raw: Any) -> dict[str, Any]:
    """Normalize the user-facing routing section and tolerate bad values."""
    cfg = _mapping(raw)
    profiles = _mapping(cfg.get("profiles") or cfg.get("model_profiles"))
    if not profiles:
        profiles = {name: cfg.get(name) for name in ROUTE_NAMES}
    normalized_profiles = {}
    for name in ROUTE_NAMES:
        slot = _mapping(profiles.get(name))
        provider = str(slot.get("provider") or "").strip()
        model = str(slot.get("model") or "").strip()
        if provider and model:
            normalized_profiles[name] = {
                "provider": provider,
                "model": model,
                **({"base_url": str(slot["base_url"])} if slot.get("base_url") else {}),
                **({"api_key_env": str(slot["api_key_env"])} if slot.get("api_key_env") else {}),
            }

    classifier = _mapping(cfg.get("classifier"))
    if not classifier:
        classifier = {
            "provider": cfg.get("classifier_provider"),
            "model": cfg.get("classifier_model"),
        }
    classifier_config = {
        "provider": str(classifier.get("provider") or "").strip(),
        "model": str(classifier.get("model") or "").strip(),
        "base_url": str(classifier.get("base_url") or "").strip(),
        "api_key_env": str(classifier.get("api_key_env") or "").strip(),
        "api_mode": str(classifier.get("api_mode") or "").strip() or None,
        "prompt": str(classifier.get("prompt") or DEFAULT_CLASSIFIER_PROMPT),
    }
    trace = _mapping(cfg.get("trace"))
    return {
        "enabled": _bool(cfg.get("enabled"), False),
        "classifier": classifier_config,
        "profiles": normalized_profiles,
        "moa": _mapping(cfg.get("moa")),
        "trace": {
            "enabled": _bool(trace.get("enabled"), True),
            "dir": trace.get("dir"),
            "retention_days": _positive_int(trace.get("retention_days"), 7),
        },
    }


def _scoped_secret(name: str) -> Optional[str]:
    if not name:
        return None
    from agent.secret_scope import get_secret

    return get_secret(name)


def _response_text(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        )
    return str(content or "")


def _parse_decision(raw_output: str, *, initial: bool) -> Optional[dict[str, Any]]:
    """Parse the strict classifier contract.

    The legacy exact ``moa`` response is retained as a compatibility bridge,
    but it always carries ``use_moa=True`` and the required Terra fallback base.
    Arbitrary prose is invalid and fails closed to the caller's fallback rule.
    """
    text = str(raw_output or "").strip()
    if text.lower() == "moa":
        return {"base_route": "terra", "use_moa": True} if initial else {"use_moa": True}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("use_moa"), bool):
        return None
    if initial:
        base_route = value.get("base_route")
        if base_route not in ROUTE_NAMES:
            return None
        return {"base_route": base_route, "use_moa": value["use_moa"]}
    return {"use_moa": value["use_moa"]}


def _trace_dir(cfg: dict[str, Any]) -> Optional[Path]:
    trace = cfg.get("trace") or {}
    if not trace.get("enabled"):
        return None
    raw = trace.get("dir")
    return (
        Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if raw
        else get_hermes_home() / "routing-traces"
    )


def _trace(session_id: Optional[str], cfg: dict[str, Any], record: dict[str, Any]) -> None:
    base = _trace_dir(cfg)
    if base is None:
        return
    try:
        base.mkdir(parents=True, exist_ok=True)
        base.chmod(0o700)
        safe_id = "".join(
            c if c.isalnum() or c in "-_." else "_"
            for c in str(session_id or "unknown")
        )
        path = base / f"routing-{safe_id}.jsonl"
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"ts": time.time(), **record},
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
        cutoff = time.time() - cfg["trace"]["retention_days"] * 86400
        for candidate in base.glob("routing-*.jsonl"):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                pass
    except Exception:
        logger.debug("smart routing trace write failed", exc_info=True)


def _call_classifier(cfg: dict[str, Any], message: str) -> Any:
    """Call the configured classifier through the auxiliary LLM contract."""
    from agent.auxiliary_client import call_llm

    classifier = cfg["classifier"]
    return call_llm(
        provider=classifier["provider"],
        model=classifier["model"],
        base_url=classifier["base_url"] or None,
        api_key=_scoped_secret(classifier["api_key_env"]),
        api_mode=classifier["api_mode"],
        messages=[
            {"role": "system", "content": classifier["prompt"]},
            {"role": "user", "content": message},
        ],
        temperature=0,
        max_tokens=32,
    )


def resolve_gateway_turn_route(
    *,
    message: str,
    config: Any,
    primary: dict[str, Any],
    session_id: Optional[str],
    state: Optional[dict[str, Any]],
    classifier: Optional[Callable[..., Any]] = None,
    runtime_resolver: Optional[Callable[[str], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Resolve one route.

    This function is synchronous by design for easy unit testing, but the
    gateway calls it only through ``asyncio.to_thread``. It never mutates the
    conversation or the cached agent.
    """
    cfg = normalize_model_routing_config(config)
    state = state or {}
    base_model = primary.get("model")
    base_runtime = {
        key: value for key, value in primary.items() if key != "model"
    }
    result = {
        "model": base_model,
        "runtime": base_runtime,
        "decision": "disabled",
        "base_profile": state.get("base_profile"),
        "use_moa": False,
        "one_shot_moa": False,
    }
    if (
        not cfg["enabled"]
        or not cfg["classifier"]["provider"]
        or not cfg["classifier"]["model"]
    ):
        return result

    initial = not state.get("base_profile")
    prompt = f"{cfg['classifier']['prompt']}\n\nUser request:\n{message}"
    started = time.perf_counter()
    raw_output = ""
    usage = None
    error = None
    parsed = None
    try:
        response = (
            classifier(prompt=prompt, config=cfg["classifier"])
            if classifier is not None
            else _call_classifier(cfg, message)
        )
        raw_output = _response_text(response) if hasattr(response, "choices") else str(response or "")
        usage_obj = getattr(response, "usage", None)
        if usage_obj is not None:
            usage = {
                key: getattr(usage_obj, key, None)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "input_tokens",
                    "output_tokens",
                )
                if getattr(usage_obj, key, None) is not None
            }
        parsed = _parse_decision(raw_output, initial=initial)
        if parsed is None:
            raise ValueError("classifier output was not valid routing JSON")
    except Exception as exc:
        error = str(exc)
        parsed = (
            {"base_route": "terra", "use_moa": False}
            if initial
            else {"use_moa": False}
        )

    base_profile = (
        parsed.get("base_route")
        if initial
        else state.get("base_profile")
    )
    if base_profile not in ROUTE_NAMES:
        base_profile = "terra"
    use_moa = bool(parsed.get("use_moa"))
    result["base_profile"] = base_profile
    result["use_moa"] = use_moa
    result["one_shot_moa"] = use_moa
    result["decision"] = (
        f"{base_profile}+moa" if use_moa else base_profile
    ) if initial else ("moa" if use_moa else f"{base_profile}_pinned")

    route = cfg["profiles"].get(base_profile)
    if route and not use_moa:
        result["model"] = route["model"]
        result["runtime"] = dict(base_runtime)
        result["runtime"].update(
            {key: route[key] for key in ("provider", "base_url") if key in route}
        )
        if runtime_resolver is not None:
            try:
                result["runtime"].update(runtime_resolver(route["provider"]))
            except Exception as exc:
                error = error or str(exc)
                result["model"] = base_model
                result["runtime"] = base_runtime
                result["decision"] = "terra_fallback" if initial else f"{base_profile}_pinned_fallback"
        if route.get("base_url"):
            result["runtime"]["base_url"] = route["base_url"]
        if route.get("api_key_env"):
            result["runtime"]["api_key"] = _scoped_secret(route["api_key_env"])

    _trace(
        session_id,
        cfg,
        {
            "prompt": prompt,
            "output": raw_output,
            "parsed_decision": parsed,
            "decision": result["decision"],
            "base_profile": result["base_profile"],
            "use_moa": result["use_moa"],
            "initial": initial,
            "error": error,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "usage": usage,
        },
    )
    return result
