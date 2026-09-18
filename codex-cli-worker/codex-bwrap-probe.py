#!/usr/bin/python3
"""Normalize only Codex's harmless Bubblewrap /proc preflight diagnostics.

Temporary compatibility for openai/codex#44329 (also present in CLI 0.155.0).
Remove once the pinned CLI recognizes both /proc and /newroot/proc failures.
Normal task commands never enter this helper; codex-bwrap execs them directly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

REAL_BWRAP = "/opt/codex-sandbox/bwrap.real"
PROC_ERRORS = (
    b"Invalid argument",
    b"Operation not permitted",
    b"Permission denied",
)


def normalize_proc_error(stderr: bytes) -> bytes:
    """Replace complete known error lines, preserving all other bytes."""
    lines = stderr.splitlines(keepends=True)
    for index, line in enumerate(lines):
        for error in PROC_ERRORS:
            old = b"bwrap: Can't mount proc on /proc: " + error
            if line.rstrip(b"\r\n") == old:
                lines[index] = line.replace(b"on /proc:", b"on /newroot/proc:", 1)
                break
    return b"".join(lines)


def main() -> int:
    # Keep inherited descriptors: Codex can pass descriptor-backed mounts.
    # stdout/stdin stay inherited; only the known probe's stderr is captured.
    with subprocess.Popen(
        [REAL_BWRAP, "--cap-drop", "ALL", *sys.argv[1:]],
        stderr=subprocess.PIPE,
        close_fds=False,
    ) as child:
        previous_handlers = {}

        def forward_signal(signum: int, _frame: object) -> None:
            child.send_signal(signum)

        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, forward_signal)
        try:
            _, stderr = child.communicate()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)

    returncode = child.returncode
    # Success is never reclassified, and unrelated failures remain failures.
    sys.stderr.buffer.write(normalize_proc_error(stderr) if returncode > 0 else stderr)
    sys.stderr.buffer.flush()
    if returncode < 0:
        # Preserve signal termination, not just a shell-style numeric status.
        signum = -returncode
        if signum not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        return 128 + signum
    return returncode


if __name__ == "__main__":
    sys.exit(main())
