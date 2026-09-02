"""Archive trusted local message media at the end of each agent turn."""

from __future__ import annotations

import logging
from typing import Any

from gateway.message_media import project_message_media
from hermes_state import SessionDB


logger = logging.getLogger(__name__)


def _archive_session_media(*, session_id: str = "", **_kwargs: Any) -> None:
    if not session_id:
        return
    db = None
    try:
        db = SessionDB(read_only=True)
        messages = db.get_messages(session_id)
        project_message_media(messages, session_id=session_id)
    except Exception as exc:
        logger.warning(
            "Message media archival failed for session %s: %s",
            session_id,
            exc,
        )
    finally:
        if db is not None:
            db.close()


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_end", _archive_session_media)
