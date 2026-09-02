"""Tests for Dashboard authentication via the Gateway API key."""

from __future__ import annotations

from hermes_cli.dashboard_auth import (
    TokenPrincipal,
    assert_protocol_compliance,
    clear_providers,
)
from hermes_cli.dashboard_auth.token_auth import (
    clear_token_routes,
    is_token_route,
)
from plugins.dashboard_auth import gateway_key


class _Context:
    def __init__(self) -> None:
        self.provider = None

    def register_dashboard_auth_provider(self, provider) -> None:
        self.provider = provider


def setup_function() -> None:
    clear_providers()
    clear_token_routes()


def teardown_function() -> None:
    clear_providers()
    clear_token_routes()


def test_provider_accepts_only_the_configured_gateway_key():
    assert_protocol_compliance(gateway_key.GatewayApiKeyProvider)
    provider = gateway_key.GatewayApiKeyProvider(secret="gateway-secret")

    principal = provider.verify_token(token="gateway-secret")

    assert isinstance(principal, TokenPrincipal)
    assert principal.scopes == ("dashboard",)
    assert provider.verify_token(token="wrong") is None


def test_register_enables_only_selected_dashboard_api_routes(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "gateway-secret")
    context = _Context()

    gateway_key.register(context)

    assert isinstance(context.provider, gateway_key.GatewayApiKeyProvider)
    assert is_token_route("/api/model/info")
    assert is_token_route("/api/cron/jobs/123/trigger")
    assert is_token_route("/api/audio/transcribe")
    assert is_token_route("/api/auth/ws-ticket")
    assert not is_token_route("/api/status")
    assert not is_token_route("/api/fs")
