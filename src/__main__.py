"""Entry point.

A restart is done by re-executing the process, not by looping over
``asyncio.run(main())``.

``asyncio.run`` finishes by cancelling whatever tasks are still pending,
shutting down async generators and closing the loop.  Some of what the bridge
leaves behind never finishes — pymax keeps socket threads that pymax itself
warns are unreliable on Python 3.12 — so that teardown can block forever.
``main()`` has already completed its own shutdown and logged "Stopped." by
then, so the symptom was a live process doing nothing at all: the bridge
stayed down until someone ssh'd into the server, which is exactly what the
in-Telegram /restart exists to avoid (2026-09-04).

``run_until_complete`` returns as soon as ``main()`` does and never waits on
leftover tasks, so control reliably comes back here — and ``execv`` then
replaces the whole process image, which discards those tasks and threads
outright.  State is already flushed at that point: ``main()`` stops the pools,
the listeners and the message store before returning.
"""

import asyncio
import os
import sys

from .main import main

# Survives execv, so the new process knows it is a restart rather than a boot.
_RESTART_ENV = "BRIDGE_IS_RESTART"


def _flush() -> None:
    sys.stdout.flush()
    sys.stderr.flush()


def run() -> None:
    is_restart = os.environ.pop(_RESTART_ENV, "") == "1"

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    restart = loop.run_until_complete(main(is_restart=is_restart))

    if restart:
        os.environ[_RESTART_ENV] = "1"
        _flush()
        # Re-exec as `python -u -m src` — argv[0] under -m is the path to this
        # file, and running that directly would break the relative imports.
        os.execv(sys.executable, [sys.executable, "-u", "-m", "src", *sys.argv[1:]])

    # Skip the interpreter's own teardown for the same reason it is skipped
    # above: leftover pymax threads can block it, and there is nothing left to
    # clean up that main() has not already done.
    _flush()
    os._exit(0)


if __name__ == "__main__":
    run()
