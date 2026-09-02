"""Authenticated voice ingress for Hermes messaging sessions."""

from __future__ import annotations

__all__ = ["register"]


def check_requirements() -> bool:
    return True


def _configured_value(name: str) -> str:
    from hermes_cli.config import get_env_value

    return (get_env_value(name) or "").strip()


def _env_enablement() -> dict[str, str]:
    return {
        "bearer_token": _configured_value("VOICE_INGRESS_BEARER_TOKEN"),
        "target_user_id": (
            _configured_value("VOICE_INGRESS_WEIXIN_USER_ID")
            or _configured_value("WEIXIN_HOME_CHANNEL")
        ),
        "target_chat_id": _configured_value("VOICE_INGRESS_WEIXIN_CHAT_ID"),
        "port": _configured_value("VOICE_INGRESS_PORT"),
        "cache_hours": _configured_value("VOICE_INGRESS_CACHE_HOURS"),
    }


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("bearer_token") and extra.get("target_user_id"))


def is_connected(_config) -> bool:
    return validate_config(_config)


def register(ctx) -> None:
    from .adapter import VoiceIngressAdapter

    ctx.register_platform(
        name="voice_ingress",
        label="Voice Ingress",
        adapter_factory=lambda cfg: VoiceIngressAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        env_enablement_fn=_env_enablement,
        required_env=["VOICE_INGRESS_BEARER_TOKEN"],
        install_hint="C.ONE packet input additionally requires the system libopus runtime.",
        emoji="🎙️",
        allowed_users_env="VOICE_INGRESS_ALLOWED_USERS",
        allow_all_env="VOICE_INGRESS_ALLOW_ALL_USERS",
        allow_update_command=False,
    )
