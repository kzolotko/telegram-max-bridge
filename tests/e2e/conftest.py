"""
pytest-asyncio fixtures for E2E tests.

Provides a shared `harness` fixture (session-scoped) that connects both
TG and MAX test clients once and reuses them across all tests in the session.

After each test session, TEST_CASES.md is updated with the latest results.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio

from .config import load_e2e_config
from .harness import E2EHarness


# ── Constants ─────────────────────────────────────────────────────────────────

_RESULTS: dict[str, tuple[str, str]] = {}  # case_id -> (status_emoji, last_run_note)
_CASE_ID_RE = re.compile(r"::test_([A-Z]\d+)_")
_TEST_CASES_MD = Path(__file__).parent / "TEST_CASES.md"


# ── Pytest hooks ──────────────────────────────────────────────────────────────

def pytest_configure(config):
    """Set up logging and register custom markers for E2E tests."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy loggers
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("pymax").setLevel(logging.WARNING)

    # Register custom markers
    config.addinivalue_line("markers", "tg_to_max: TG→MAX direction tests")
    config.addinivalue_line("markers", "max_to_tg: MAX→TG direction tests")
    config.addinivalue_line("markers", "formatting: formatting tests")
    config.addinivalue_line("markers", "text: plain text tests")
    config.addinivalue_line("markers", "reply: reply tests")
    config.addinivalue_line("markers", "edit: edit tests")
    config.addinivalue_line("markers", "delete: delete tests")
    config.addinivalue_line("markers", "echo: echo loop tests")
    config.addinivalue_line("markers", "edge: edge case tests")
    config.addinivalue_line("markers", "reaction: reaction tests")
    config.addinivalue_line("markers", "media: media/photo/video/album tests")
    config.addinivalue_line("markers", "twouser: tests requiring second user")
    config.addinivalue_line("markers", "dm: DM bridge tests")


def pytest_runtest_logreport(report):
    """Capture test results for MD update."""
    if report.when != "call":
        return
    m = _CASE_ID_RE.search(report.nodeid)
    if not m:
        return
    case_id = m.group(1)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if report.passed:
        _RESULTS[case_id] = ("✅", timestamp)
    elif report.failed:
        error_note = _extract_error(report)
        note = f"{timestamp} — {error_note}" if error_note else timestamp
        _RESULTS[case_id] = ("❌", note[:100])
    elif report.skipped:
        _RESULTS[case_id] = ("⚠️", f"{timestamp} (skipped)")


def pytest_sessionfinish(session, exitstatus):
    """Update TEST_CASES.md with test results after session completes."""
    if not _RESULTS or not _TEST_CASES_MD.exists():
        return
    _update_md(_RESULTS)


# ── MD update helpers ─────────────────────────────────────────────────────────

def _extract_error(report) -> str:
    """Extract a short error message from a failed report."""
    if not hasattr(report, "longrepr") or not report.longrepr:
        return ""
    text = str(report.longrepr)
    for line in reversed(text.split("\n")):
        line = line.strip()
        if line.startswith("AssertionError:"):
            return line[len("AssertionError:"):].strip()[:80]
        if line.startswith("E  AssertionError:"):
            return line[len("E  AssertionError:"):].strip()[:80]
    return ""


def _update_md(results: dict):
    """Parse TEST_CASES.md and update status + last_run columns in-place."""
    lines = _TEST_CASES_MD.read_text(encoding="utf-8").splitlines()
    updated = []
    for line in lines:
        updated.append(_update_row(line, results))
    _TEST_CASES_MD.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _update_row(line: str, results: dict) -> str:
    """If this line is a test-case table row matching a result, update it."""
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return line
    parts = stripped.split("|")
    # parts[0] = '', parts[1..N-1] = cells, parts[-1] = ''
    inner = parts[1:-1]
    if len(inner) < 5:
        return line
    case_id = inner[0].strip()
    if not re.match(r'^[A-Z]\d+$', case_id):
        return line
    if case_id not in results:
        return line
    status, last_run = results[case_id]
    # Update columns 3 (Статус) and 4 (Последний прогон)
    inner[3] = f" {status} "
    inner[4] = f" {last_run} "
    return "|" + "|".join(inner) + "|"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def e2e_config():
    """Load E2E configuration (once per test session)."""
    return load_e2e_config()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def harness(e2e_config):
    """Session-scoped E2E harness with connected TG and MAX test clients.

    Starts both clients before the first test, stops after the last.
    """
    h = E2EHarness(e2e_config)
    await h.start()
    yield h
    await h.stop()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _drain_queues(harness):
    """Drain stale events and add a brief pause between tests.

    The drain prevents leftover messages from a previous test from leaking
    into the next one.  The pause avoids MAX rate limits on edits/deletes
    (``errors.edit-message.send-too-many-edit``) that cause cascading
    failures when tests run back-to-back.
    """
    harness.tg.drain()
    harness.max.drain()
    if harness.tg2:
        harness.tg2.drain()
    if harness.max2:
        harness.max2.drain()
    if harness.tg_bot_chat:
        harness.tg_bot_chat.drain()
    if harness.max_dm:
        harness.max_dm.drain()
    await asyncio.sleep(1)
    yield
