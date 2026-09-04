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


async def test_start_survives_a_dead_token(session_dir):
    """A revoked token must not propagate out of start()."""
    err = RuntimeError("FAIL_LOGIN_TOKEN [login.token]")
    listener, calls = _make_listener(session_dir, connect_error=err)

    user_id = await listener.start()

    assert user_id == 777
    assert calls == [1]
    # The reconnect loop must be running so the listener recovers by itself
    # once the token is valid again.
    assert listener._monitor_task is not None
    assert not listener._monitor_task.done()
    assert listener._worker_task is not None
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
