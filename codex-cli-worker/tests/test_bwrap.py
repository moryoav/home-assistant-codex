"""Exercise the real wrapper using a fake Bubblewrap, without namespaces."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

WORKER = Path(__file__).resolve().parents[1]
PROC_ERROR = b"bwrap: Can't mount proc on /proc: Operation not permitted\n"
RECOGNIZED_ERROR = PROC_ERROR.replace(b"on /proc:", b"on /newroot/proc:")
PROBE = [
    "--as-pid-1", "--new-session", "--die-with-parent",
    "--tmpfs", "/", "--ro-bind", "/usr", "/usr", "--dev", "/dev",
    "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-net",
    "--proc", "/proc", "--cap-drop", "ALL", "--", "/usr/bin/true",
]


class BubblewrapCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.record = root / "record.json"
        self.real = root / "bwrap.real"
        self.real.write_text(
            f"#!{sys.executable}\n"
            "import json, os, signal, sys\n"
            "record = {'args': sys.argv[1:], 'pid': os.getpid()}\n"
            "if os.environ.get('TEST_FD'):\n"
            "    record['fd'] = os.read(int(os.environ['TEST_FD']), 100).decode()\n"
            "with open(os.environ['TEST_RECORD'], 'w') as f:\n"
            "    json.dump(record, f)\n"
            "os.write(1, bytes.fromhex(os.environ.get('TEST_STDOUT', '')))\n"
            "os.write(2, bytes.fromhex(os.environ.get('TEST_STDERR', '')))\n"
            "if os.environ.get('TEST_SIGNAL'):\n"
            "    os.kill(os.getpid(), int(os.environ['TEST_SIGNAL']))\n"
            "sys.exit(int(os.environ.get('TEST_EXIT', '0')))\n",
            encoding="utf-8",
        )
        self.real.chmod(0o755)
        helper = root / "codex-bwrap-probe.py"
        helper.write_text(
            (WORKER / "codex-bwrap-probe.py").read_text(encoding="utf-8")
            .replace('REAL_BWRAP = "/opt/codex-sandbox/bwrap.real"',
                     f"REAL_BWRAP = {str(self.real)!r}"),
            encoding="utf-8",
        )
        self.wrapper = root / "bwrap"
        # Change trusted paths only in a private test copy, never via a runtime
        # environment override that could redirect production sandbox setup.
        self.wrapper.write_text(
            (WORKER / "codex-bwrap").read_text(encoding="utf-8")
            .replace("REAL_BWRAP=/opt/codex-sandbox/bwrap.real", f"REAL_BWRAP={self.real}")
            .replace("/usr/bin/python3 /opt/codex-sandbox/codex-bwrap-probe.py",
                     f"{sys.executable} {helper}"),
            encoding="utf-8",
        )
        self.wrapper.chmod(0o755)

    def run_wrapper(
        self, args: list[str], *, stderr: bytes = PROC_ERROR,
        stdout: bytes = b"", exit_code: int = 1, **extra: str,
    ) -> tuple[subprocess.CompletedProcess[bytes], dict]:
        result = subprocess.run(
            [str(self.wrapper), *args],
            env={**os.environ, "TEST_RECORD": str(self.record),
                 "TEST_STDERR": stderr.hex(), "TEST_STDOUT": stdout.hex(),
                 "TEST_EXIT": str(exit_code), **extra},
            capture_output=True, timeout=5, check=False,
        )
        return result, json.loads(self.record.read_text(encoding="utf-8"))

    def test_normalizes_only_recognized_failed_probe_lines(self) -> None:
        for true in ("/bin/true", "/usr/bin/true"):
            for error in (b"Operation not permitted", b"Permission denied", b"Invalid argument"):
                for newline in (b"\n", b"\r\n", b""):
                    with self.subTest(true=true, error=error, newline=newline):
                        diagnostic = b"bwrap: Can't mount proc on /proc: " + error + newline
                        result, record = self.run_wrapper(
                            [*PROBE[:-1], true], stderr=diagnostic, exit_code=17,
                            stdout=b"unchanged stdout\x00\xff",
                        )
                        self.assertEqual(result.returncode, 17)
                        self.assertEqual(result.stderr, diagnostic.replace(b"on /proc:", b"on /newroot/proc:"))
                        self.assertEqual(result.stdout, b"unchanged stdout\x00\xff")
                        self.assertEqual(record["args"], ["--cap-drop", "ALL", *PROBE[:-1], true])

    def test_success_and_unrelated_errors_are_unchanged(self) -> None:
        cases = [
            (PROC_ERROR, 0), (RECOGNIZED_ERROR, 1),
            (b"bwrap: Creating new namespace failed: Operation not permitted\n", 1),
            (b"bwrap: Can't mount proc on /proc: No such file or directory\n", 1),
            (b"prefix: " + PROC_ERROR, 1), (b"\xff unrelated\x00", 1),
            (PROC_ERROR.rstrip() + b" (extra detail)\n", 1),
        ]
        for diagnostic, code in cases:
            with self.subTest(diagnostic=diagnostic, code=code):
                result, _ = self.run_wrapper(PROBE, stderr=diagnostic, exit_code=code)
                self.assertEqual(result.stderr, diagnostic)
                self.assertEqual(result.returncode, code)

    def test_preserves_other_lines_around_recognized_failure(self) -> None:
        result, _ = self.run_wrapper(PROBE, stderr=b"before\n" + PROC_ERROR + b"after")
        self.assertEqual(result.stderr, b"before\n" + RECOGNIZED_ERROR + b"after")

    def test_real_commands_and_lookalikes_bypass_helper(self) -> None:
        separator = PROBE.index("--")
        cases = [
            [*PROBE[:separator + 1], "/usr/local/bin/codex", "--apply-seccomp-then-exec", "--", "/bin/true"],
            [*PROBE, "extra"], [*PROBE[:-1], "true"],
            ["--unshare-user", "--unshare-pid", "--unshare-net", "--ro-bind", "/", "/", "/bin/true"],
            [*PROBE[:separator + 1], "/bin/sh", "-c", " ".join(PROBE)],
        ]
        for flag in ("--as-pid-1", "--new-session", "--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc"):
            cases.append([arg for arg in PROBE if arg != flag])
        proc_index = PROBE.index("--proc")
        cases.append([*PROBE[:proc_index], *PROBE[proc_index + 2:]])
        cases.append([*PROBE[:proc_index + 1], "/elsewhere", *PROBE[proc_index + 2:]])
        for args in cases:
            with self.subTest(args=args):
                result, record = self.run_wrapper(args, exit_code=19)
                self.assertEqual(result.stderr, PROC_ERROR)
                self.assertEqual(result.returncode, 19)
                self.assertEqual(record["args"], ["--cap-drop", "ALL", *args])

    def test_version_and_help_remain_usable(self) -> None:
        for option in ("--version", "--help"):
            result, record = self.run_wrapper([option], stderr=b"", stdout=b"supported options\n", exit_code=0)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"supported options\n")
            expected = [option] if option == "--version" else ["--cap-drop", "ALL", option]
            self.assertEqual(record["args"], expected)

    def test_normal_path_exec_preserves_pid(self) -> None:
        with subprocess.Popen(
            [str(self.wrapper), "--", "/bin/true"],
            env={**os.environ, "TEST_RECORD": str(self.record)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as child:
            child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(json.loads(self.record.read_text())["pid"], child.pid)

    def test_probe_preserves_inherited_mount_descriptors(self) -> None:
        with tempfile.TemporaryFile() as file:
            file.write(b"mount descriptor")
            file.seek(0)
            result = subprocess.run(
                [str(self.wrapper), *PROBE],
                env={**os.environ, "TEST_RECORD": str(self.record), "TEST_FD": str(file.fileno())},
                pass_fds=(file.fileno(),), capture_output=True, timeout=5,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.record.read_text())["fd"], "mount descriptor")

    def test_probe_preserves_signal_termination(self) -> None:
        result, _ = self.run_wrapper(PROBE, TEST_SIGNAL=str(signal.SIGTERM))
        self.assertEqual(result.returncode, -signal.SIGTERM)
        self.assertEqual(result.stderr, PROC_ERROR)


if __name__ == "__main__":
    unittest.main()
