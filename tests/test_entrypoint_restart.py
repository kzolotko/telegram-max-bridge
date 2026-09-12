"""The entry point must not hang when it restarts.

Regression cover for the 2026-09-04 incident: /restart from the admin bot was
acknowledged, main() ran its shutdown and logged "Stopped." — and then the
process sat there alive and idle forever, so the bridge stayed down until
someone ssh'd in.  The old entry point looped over ``asyncio.run(main())``,
whose teardown calls ``loop.shutdown_default_executor()`` and joins executor
threads; pymax leaves blocking ones behind, so that join never returned.

These tests exercise the real ``src/__main__.py``, rewritten only to name a
throwaway package (so the re-exec does not launch an actual bridge) and to
import a stub ``main``.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "src" / "__main__.py"

# A main() that leaves a blocking executor thread behind, as the bridge does,
# and asks for exactly one restart.
STUB_MAIN = '''
import asyncio, os, time

async def main(is_restart: bool = False):
    print(f"RUN is_restart={is_restart}", flush=True)
    asyncio.get_running_loop().run_in_executor(None, time.sleep, 3600)
    await asyncio.sleep(0.05)
    print("Stopped.", flush=True)
    return not is_restart
'''

OLD_ENTRYPOINT = '''
from .main import main
import asyncio

is_restart = False
while True:
    restart = asyncio.run(main(is_restart=is_restart))
    if not restart:
        break
    is_restart = True
'''


def _make_pkg(tmp_path: Path, name: str, entry_src: str) -> Path:
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text(textwrap.dedent(STUB_MAIN))
    (pkg / "__main__.py").write_text(entry_src)
    return pkg


def _run(tmp_path: Path, name: str, timeout: float = 15):
    return subprocess.run(
        [sys.executable, "-u", "-m", name],
        cwd=tmp_path, capture_output=True, text=True, timeout=timeout,
    )


def test_restart_reexecs_once_and_exits(tmp_path):
    """The shipped entry point restarts once, then exits — without hanging."""
    src = ENTRYPOINT.read_text().replace('"-m", "src"', '"-m", "pkg_new"')
    _make_pkg(tmp_path, "pkg_new", src)

    proc = _run(tmp_path, "pkg_new")

    assert proc.returncode == 0
    runs = [ln for ln in proc.stdout.splitlines() if ln.startswith("RUN ")]
    assert runs == ["RUN is_restart=False", "RUN is_restart=True"], proc.stdout


def test_old_asyncio_run_loop_would_hang(tmp_path):
    """Control: the pattern this replaced does hang on the same stub."""
    _make_pkg(tmp_path, "pkg_old", OLD_ENTRYPOINT)

    with pytest.raises(subprocess.TimeoutExpired):
        _run(tmp_path, "pkg_old", timeout=8)
