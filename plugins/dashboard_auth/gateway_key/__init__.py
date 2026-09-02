"""Authenticate selected Dashboard APIs with the Gateway API key."""

from __future__ import annotations

import hmac
import os
from typing import Any, Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)
from hermes_cli.dashboard_auth.token_auth import (
    register_token_route,
    register_token_route_prefix,
)


EXACT_ROUTES = (
    "/api/auth/ws-ticket",
    "/api/config",
    "/api/memory",
    "/api/skills",
)
ROUTE_PREFIXES = (
    "/api/audio/",
    "/api/config/",
    "/api/cron/",
    "/api/memory/",
    "/api/model/",
    "/api/skills/",
)


class GatewayApiKeyProvider(DashboardAuthProvider):
    """Token-only provider backed by the existing API_SERVER_KEY."""

    name = "gateway-api-key"
    display_name = "Gateway API Key"
    supports_token = True
    supports_session = False

    def __init__(self, *, secret: str) -> None:
        if not secret:
            raise ValueError("Gateway API key must not be empty")
        self._secret = secret

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if not token:
            return None
        if not hmac.compare_digest(
            token.encode("utf-8"),
            self._secret.encode("utf-8"),
        ):
            return None
        return TokenPrincipal(
            principal="gateway-client",
            provider=self.name,
            scopes=("dashboard",),
        )

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError

    def complete_login(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Session:
        raise NotImplementedError

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        raise NotImplementedError

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError

    def revoke_session(self, *, refresh_token: str) -> None:
        raise NotImplementedError


def register(ctx: Any) -> None:
    secret = os.environ.get("API_SERVER_KEY", "").strip()
    if not secret:
        return
    ctx.register_dashboard_auth_provider(GatewayApiKeyProvider(secret=secret))
    for route in EXACT_ROUTES:
        register_token_route(route)
    for prefix in ROUTE_PREFIXES:
        register_token_route_prefix(prefix)
