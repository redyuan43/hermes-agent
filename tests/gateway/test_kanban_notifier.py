import asyncio
import sqlite3
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb

_REAL_ASYNCIO_SLEEP = asyncio.sleep


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await _REAL_ASYNCIO_SLEEP(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_replays_telegram_dm_topic_delivery_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "dm-topic-metadata.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm topic task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "20197",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
                "thread_id": "20197",
            },
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"
    assert adapter.handled[0].source.thread_id == "20197"


def test_active_named_profile_subscription_is_delivered(tmp_path, monkeypatch):
    """A sub stamped with the gateway's own named profile uses self.adapters.

    Regression for #71340: on a standalone (non-multiplex) gateway running a
    named profile, _authorization_adapter() used to treat the active name as a
    multiplex secondary, find no _profile_adapters entry, fail closed, and
    rewind the claim forever — silent zero-delivery.
    """
    db_path = tmp_path / "actionable-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    reason = "AGE-39 — https://linear.example/AGE-39 — publishing verified."
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="approval", assignee="publisher")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="main",
        )
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "main"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    message = adapter.sent[0]["text"]
    assert tid in message
    assert "blocked" in message


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class ReportedFailureAdapter:
    """Adapter that REPORTS failure via SendResult(success=False) instead of
    raising — the exact contract the Telegram adapter uses for 'Not connected'
    and degraded-send paths."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Not connected")


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_wakeup_uses_subscription_chat_type(tmp_path, monkeypatch):
    db_path = tmp_path / "chat-type-wakeup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm requester",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-dm",
            chat_type="dm",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"

    # The wake must resume the creator's real DM session key — the whole bug
    # was that a hardcoded chat_type="group" made build_session_key() produce
    # a group-scoped key (a NEW session) instead of the ":dm:<chat_id>" shape
    # the original conversation runs under (#56580 / #68874).
    from gateway.session import build_session_key

    wake_key = build_session_key(adapter.handled[0].source)
    assert wake_key == "agent:main:telegram:dm:chat-dm"
    assert ":group:" not in wake_key


def test_notifier_delivers_via_secondary_profile_adapter(tmp_path, monkeypatch):
    """A multiplexed secondary profile can own and deliver its notification."""
    db_path = tmp_path / "secondary-profile-delivery.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    secondary_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    runner._profile_adapters = {"beta": {Platform.TELEGRAM: secondary_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(secondary_adapter.sent) == 1
    assert tid in secondary_adapter.sent[0]["text"]
    assert _unseen_terminal_events_for(tid, "chat-beta") == []


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


class RetryArtifactAdapter(RecordingAdapter):
    def __init__(self, fail_path):
        super().__init__()
        self.fail_path = str(fail_path)
        self.documents = []
        self._failed_once = False

    @staticmethod
    def extract_local_files(text):
        return [], text

    async def send_document(self, chat_id, file_path, metadata=None):
        self.documents.append(file_path)
        if file_path == self.fail_path and not self._failed_once:
            self._failed_once = True
            raise RuntimeError("transient artifact failure")


def test_notifier_retries_only_missing_artifact(tmp_path, monkeypatch):
    db_path = tmp_path / "artifact-checkpoints.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    kb.init_db()
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="artifact retry", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(
            conn,
            tid,
            summary="done",
            metadata={"artifacts": [str(first), str(second)]},
        )
    finally:
        conn.close()

    adapter = RetryArtifactAdapter(second)
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.documents.count(str(first)) == 1
    assert adapter.documents.count(str(second)) == 2


def test_notifier_switches_to_origin_after_three_primary_failures(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "origin-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="fallback", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="primary-chat",
            notifier_profile="beta",
            fallback_platform="telegram",
            fallback_chat_id="origin-chat",
            fallback_notifier_profile="default",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    primary = FailingAdapter()
    origin = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: origin}
    runner._profile_adapters = {"beta": {Platform.TELEGRAM: primary}}
    runner._kanban_sub_fail_counts = {}
    for _ in range(3):
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert primary.attempts == 3
    assert len(subs) == 1
    assert subs[0]["chat_id"] == "origin-chat"
    assert subs[0]["notifier_profile"] == "default"

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(origin.sent) == 1
    assert origin.sent[0]["chat_id"] == "origin-chat"


def test_notifier_routes_matrix_and_weixin_through_named_profiles(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "named-profile-routes.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        matrix_tid = kb.create_task(conn, title="matrix result", assignee="worker")
        weixin_tid = kb.create_task(conn, title="weixin result", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=matrix_tid,
            platform="matrix",
            chat_id="!room:example.test",
            notifier_profile="matrix-sender",
        )
        kb.add_notify_sub(
            conn,
            task_id=weixin_tid,
            platform="weixin",
            chat_id="wxid_target",
            notifier_profile="weixin-sender",
        )
        kb.complete_task(conn, matrix_tid, summary="matrix done")
        kb.complete_task(conn, weixin_tid, summary="weixin done")
    finally:
        conn.close()

    matrix_adapter = RecordingAdapter()
    weixin_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    runner._profile_adapters = {
        "matrix-sender": {Platform.MATRIX: matrix_adapter},
        "weixin-sender": {Platform.WEIXIN: weixin_adapter},
    }
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [item["chat_id"] for item in matrix_adapter.sent] == [
        "!room:example.test",
    ]
    assert [item["chat_id"] for item in weixin_adapter.sent] == ["wxid_target"]


def test_kanban_notifier_isolates_per_subscription_failure(tmp_path, monkeypatch):
    """One bad subscription must not block delivery for all others.

    Regression for #59269: when claim_unseen_events_for_sub raises for one
    subscription, the entire notifier tick used to abort — silently blocking
    delivery for every other subscription.
    """
    db_path = tmp_path / "isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create two tasks with subscriptions and complete both. The BAD task is
    # created first: list_notify_subs() has no ORDER BY, so SQLite's natural
    # scan returns insertion order — the failing subscription must be
    # processed BEFORE the good one or this test passes even without the
    # per-subscription isolation (the good delivery happens before the tick
    # aborts). A deterministic-order shim below removes the reliance on the
    # scan order entirely.
    conn = kb.connect()
    try:
        tid_bad = kb.create_task(conn, title="bad task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_bad, platform="telegram", chat_id="chat-bad")
        kb.complete_task(conn, tid_bad, summary="done")

        tid_good = kb.create_task(conn, title="good task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_good, platform="telegram", chat_id="chat-good")
        kb.complete_task(conn, tid_good, summary="done")
    finally:
        conn.close()

    original_claim = kb.claim_unseen_events_for_sub

    def selective_claim(conn, task_id, **kwargs):
        if task_id == tid_bad:
            raise RuntimeError("simulated DB corruption for bad task")
        return original_claim(conn, task_id=task_id, **kwargs)

    monkeypatch.setattr(kb, "claim_unseen_events_for_sub", selective_claim)

    # Force the failing subscription to be iterated FIRST regardless of the
    # unordered SELECT's scan order.
    original_list = kb.list_notify_subs

    def bad_first(conn, task_id=None):
        subs = original_list(conn, task_id)
        return sorted(subs, key=lambda s: 0 if s["task_id"] == tid_bad else 1)

    monkeypatch.setattr(kb, "list_notify_subs", bad_first)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The good task must still be delivered despite the bad task failing.
    assert len(adapter.sent) == 1
    assert tid_good in adapter.sent[0]["text"]


def test_notifier_delivers_block_loop_detected_triage_ping(tmp_path, monkeypatch):
    """A `block_loop_detected` event must reach the subscriber as a triage ping.

    Regression for the silent-triage gap (PR #62712): kanban_db routes a task
    to `triage` after BLOCK_RECURRENCE_LIMIT re-blocks for the same cause and
    emits ONLY a `block_loop_detected` event — no `blocked`/`status` event.
    Before `block_loop_detected` joined TERMINAL_KINDS with its own message
    branch, that one transition (the whole point of which is to force human
    attention) produced zero notification and the task stalled in triage
    silently.
    """
    db_path = tmp_path / "block-loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="loops forever", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "block_loop_detected must produce a notification"
    text = adapter.sent[0]["text"]
    assert "TRIAGE" in text
    assert tid in text
    assert "needs credentials" in text
    # Cursor advanced: the event is claimed and not re-delivered.
    conn = kb.connect()
    try:
        _, remaining = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["block_loop_detected"],
        )
    finally:
        conn.close()
    assert remaining == []
