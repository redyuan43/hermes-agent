from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SEND_ERROR_KINDS,
    SendResult,
    notify_message_processing_status,
)
from gateway.run import GatewayRunner


class _ActionRequiredAdapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True), Platform.WEIXIN)
        self.send_count = 0

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        del chat_id, content, reply_to, metadata
        self.send_count += 1
        return SendResult(
            success=False,
            error="User action is required",
            error_kind="action_required",
        )

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.mark.asyncio
async def test_processing_status_callback_accepts_async_observers() -> None:
    updates = []

    async def callback(stage, details):
        updates.append((stage, details))

    event = MessageEvent(text="", processing_status_callback=callback)

    await notify_message_processing_status(event, "processing", {"step": 1})

    assert updates == [("processing", {"step": 1})]


@pytest.mark.asyncio
async def test_action_required_delivery_is_not_retried() -> None:
    adapter = _ActionRequiredAdapter()

    result = await adapter._send_with_retry("chat", "reply", max_retries=3)

    assert "action_required" in SEND_ERROR_KINDS
    assert result.error_kind == "action_required"
    assert adapter.send_count == 1


def test_default_delivery_contract_does_not_expose_context() -> None:
    adapter = _ActionRequiredAdapter()
    assert adapter.delivery_ready is False
    assert adapter.delivery_context_revision("chat") is None

    adapter._running = True
    assert adapter.delivery_ready is True


def test_event_metadata_can_compose_transcript_and_suppress_echo() -> None:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(stt_echo_transcripts=True)
    event = MessageEvent(
        text="",
        metadata={
            "include_stt_transcript_in_reply": True,
            "suppress_stt_echo": True,
        },
    )
    event._gateway_reply_transcripts = ("recognized command",)

    assert runner._should_echo_stt_transcripts_for_event(event) is False
    assert runner._format_event_reply(event, "agent reply") == (
        "recognized command\n\nagent reply"
    )
