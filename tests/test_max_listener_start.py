"""Unit tests for MaxListener startup resilience.

Regression cover for the 2026-09-03 production incident: the MAX login token
was revoked (FAIL_LOGIN_TOKEN), the initial connect in ``MaxListener.start()``
raised, and that exception propagated out of ``main()`` — so the process died
and docker restarted it into a crash loop.  The admin bot never came up, which
removed the very ``/authmax`` path needed to recover.
"""

import asyncio

import pytest

from src.max.listener import MaxListener
from src.types import AppConfig, BridgeEntry, UserMapping
from src.config import ConfigLookup


class _FakeMirrorTracker:
    def is_max_mirror(self, msg_id):
        return False


def _make_listener(tmp_path, connect_error: Exception | None):
    user = UserMapping(name="alice", telegram_user_id=555, max_user_id=777)
    entry = BridgeEntry(
        name="children", telegram_chat_id=-1001, max_chat_id=2002, user=user,
    )
    config = AppConfig(
        api_id=1, api_hash="h", users=[user], bridges=[entry],
        sessions_dir=str(tmp_path),
    )

    async def on_event(event):
        pass

    listener = MaxListener(
        config=config,
        lookup=ConfigLookup(config),
        mirror_tracker=_FakeMirrorTracker(),
        on_event=on_event,
        user=user,
    )

    calls = []

    async def _fake_connect():
        calls.append(1)
        if connect_error is not None:
            raise connect_error

    listener._connect = _fake_connect
    return listener, calls


def _write_session(tmp_path, user: UserMapping):
    from src.max.session import MaxSession
    MaxSession(user.max_session, str(tmp_path)).save(
        "tok", user_id=777, device_id="0f9e6b1a-0000-4000-8000-000000000001",
    )


@pytest.fixture
def session_dir(tmp_path):
    _write_session(tmp_path, UserMapping(name="alice", telegram_user_id=555,
                                         max_user_id=777))
    return tmp_path


async def _stop(listener):
    await listener.stop()


async def test_start_survives_a_failed_connect(session_dir):
    """A transient failure (MAX unreachable) must not propagate out of start()."""
    err = TimeoutError("Send and wait failed (socket)")
    listener, calls = _make_listener(session_dir, connect_error=err)

    user_id = await listener.start()

    assert user_id == 777
    assert calls == [1]
    assert listener.auth_failed is None
    # The reconnect loop must be running so the listener recovers by itself
    # once MAX is reachable again.
    assert listener._monitor_task is not None
    assert not listener._monitor_task.done()
    assert listener._worker_task is not None
    await _stop(listener)


# ── Permanent login rejections ───────────────────────────────────────────────
#
# 2026-09-12: with a revoked token the reconnect loop retried every 60 s for
# good, MAX started answering SESSION_INIT with timeouts (throttling), and
# nobody was told — the only fix is /authmax, which needs a human.

class _PyMaxLikeError(Exception):
    """Shape of pymax.exceptions.Error: the code sits in ``.error``."""

    def __init__(self, error):
        super().__init__(f"PyMax Error: Ошибка входа {error} [login.token]")
        self.error = error


async def test_rejected_token_at_start_stops_retrying_and_notifies(session_dir):
    listener, calls = _make_listener(
        session_dir, connect_error=_PyMaxLikeError("FAIL_LOGIN_TOKEN"))
    notices = []

    async def on_auth_failure(lst, text):
        notices.append((lst, text))

    listener.on_auth_failure = on_auth_failure

    await listener.start()

    assert listener.auth_failed == "FAIL_LOGIN_TOKEN"
    # The reconnect loop exits instead of hammering MAX with a dead token.
    await asyncio.wait_for(listener._monitor_task, timeout=1)
    assert calls == [1]
    assert len(notices) == 1
    assert notices[0][0] is listener
    assert "/authmax alice" in notices[0][1]
    assert "/restart" in notices[0][1]
    await _stop(listener)


async def test_rejection_during_reconnect_stops_the_loop(session_dir, monkeypatch):
    """First connect works; the token dies later (profile mismatch here)."""
    from src.max import listener as listener_mod
    monkeypatch.setattr(listener_mod, "RECONNECT_BASE_DELAY", 0)

    listener, calls = _make_listener(session_dir, connect_error=None)
    outcomes = iter([None, RuntimeError("FAIL_WRONG_PASSWORD [login.cred]")])

    async def _connect():
        calls.append(1)
        err = next(outcomes)
        if err is not None:
            raise err

    listener._connect = _connect
    notices = []

    async def on_auth_failure(lst, text):
        notices.append(text)

    listener.on_auth_failure = on_auth_failure

    await listener.start()
    assert listener.auth_failed is None

    # client is None in this harness, so the loop goes straight to a retry.
    await asyncio.wait_for(listener._monitor_task, timeout=2)

    assert calls == [1, 1]
    assert listener.auth_failed == "FAIL_WRONG_PASSWORD [login.cred]"
    assert len(notices) == 1
    await _stop(listener)


async def test_rejected_token_without_callback_is_reported_later(session_dir):
    """Listeners start before the admin bot: main() reads the flag afterwards."""
    listener, _ = _make_listener(
        session_dir, connect_error=_PyMaxLikeError("FAIL_LOGIN_TOKEN"))

    await listener.start()

    assert listener.auth_failed == "FAIL_LOGIN_TOKEN"
    assert "/authmax alice" in listener.auth_failure_message()
    await _stop(listener)


async def test_start_still_connects_normally(session_dir):
    listener, calls = _make_listener(session_dir, connect_error=None)

    user_id = await listener.start()

    assert user_id == 777
    assert calls == [1]
    assert listener._monitor_task is not None
    await _stop(listener)


async def test_missing_session_still_raises(tmp_path):
    """A missing session file is a config error, not a transient one."""
    listener, _ = _make_listener(tmp_path, connect_error=None)
    with pytest.raises(RuntimeError, match="MAX session not found"):
        await listener.start()
