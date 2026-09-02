from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_stt_echo_transcripts_defaults_on_for_backwards_compatibility():
    cfg = GatewayConfig.from_dict({})

    assert cfg.stt_enabled is True
    assert cfg.stt_echo_transcripts is True
    assert cfg.to_dict()["stt_echo_transcripts"] is True


def test_top_level_stt_echo_transcripts_takes_precedence():
    cfg = GatewayConfig.from_dict({
        "stt_echo_transcripts": False,
        "stt": {"echo_transcripts": True},
    })

    assert cfg.stt_echo_transcripts is False


def test_events_can_suppress_transcript_echo():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(stt_echo_transcripts=True)
    event = SimpleNamespace(metadata={"suppress_stt_echo": True})

    assert runner._should_echo_stt_transcripts_for_event(event) is False


def test_regular_voice_events_keep_configured_transcript_echo():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(stt_echo_transcripts=True)
    event = SimpleNamespace(metadata={})

    assert runner._should_echo_stt_transcripts_for_event(event) is True


@pytest.mark.asyncio
async def test_event_can_combine_transcript_and_agent_response():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True, stt_echo_transcripts=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    source = SessionSource(
        platform=Platform.WEIXIN,
        chat_id="wx-user-123",
        chat_type="dm",
        user_id="wx-user-123",
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=["/tmp/c-one.wav"],
        media_types=["audio/wav"],
        metadata={
            "suppress_stt_echo": True,
            "include_stt_transcript_in_reply": True,
        },
    )

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "合并包测试一二三。",
            "provider": "agx_realtime",
        },
    ):
        prepared = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert "合并包测试一二三。" in prepared
    assert event._gateway_reply_transcripts == ("合并包测试一二三。",)
    assert runner._format_event_reply(event, "任务已经执行。") == (
        "合并包测试一二三。\n\n"
        "任务已经执行。"
    )


def test_regular_weixin_reply_is_not_reformatted():
    event = SimpleNamespace(metadata={})

    assert GatewayRunner._format_event_reply(event, "普通回复") == "普通回复"
