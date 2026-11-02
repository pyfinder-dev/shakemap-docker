"""Host checks for persistent diagnostics without changing job-log ownership."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import finalization, paths, service_logging


PROJECT = Path(__file__).resolve().parents[1]


class ServiceLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = mock.patch.object(
            paths,
            "settings",
            dataclasses.replace(paths.settings, runtime_root=str(self.root)),
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def test_append_preserves_previous_records_and_keeps_log_roles_separate(self) -> None:
        service_logging.append_record("service", "startup")
        service_logging.append_record("service", "runtime")
        service_logging.record_operation("Finalization starting.")
        self.assertEqual(
            (paths.logs_dir() / "service.log").read_text(),
            "startup\nruntime\n",
        )
        self.assertIn(
            "Finalization starting.",
            (paths.logs_dir() / "operations.log").read_text(),
        )
        self.assertEqual(
            (paths.logs_dir() / "service.log").stat().st_mode & 0o777,
            0o600,
        )
        self.assertFalse(paths.events_dir().exists())

    def test_unsafe_log_file_or_directory_never_receives_bytes(self) -> None:
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                logs = paths.logs_dir()
                logs.mkdir(parents=True, exist_ok=True)
                target = logs / "service.log"
                outside = self.root / f"outside-{kind}"
                outside.write_text("preserved")
                if kind == "symlink":
                    target.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, target)
                elif kind == "fifo":
                    os.mkfifo(target)
                else:
                    target.mkdir()
                try:
                    with self.assertRaisesRegex(ValueError, "service.log"):
                        service_logging.append_record("service", "unexpected")
                    self.assertEqual(outside.read_text(), "preserved")
                finally:
                    if kind == "directory":
                        target.rmdir()
                    else:
                        target.unlink()

    def test_concurrent_partial_writes_keep_complete_records_together(self) -> None:
        write = os.write
        messages = [f"record-{index}:" + "x" * 300 for index in range(12)]
        with mock.patch.object(
            os, "write", side_effect=lambda fd, data: write(fd, data[:17])
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(
                    lambda message: service_logging.append_record("service", message),
                    messages,
                ))
        self.assertCountEqual(
            (paths.logs_dir() / "service.log").read_text().splitlines(),
            messages,
        )

    def test_unsafe_ancestry_and_permission_failures_identify_destination(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        paths.service_root().symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "logs.*unsafe"):
            service_logging.append_record("service", "unexpected")
        self.assertEqual(list(outside.iterdir()), [])
        paths.service_root().unlink()
        with mock.patch.object(
            service_logging,
            "open_service_directory",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaisesRegex(ValueError, "service.log.*denied"):
                service_logging.append_record("service", "unexpected")

    def test_precise_finalization_error_remains_persistent_with_original_exit(self) -> None:
        with mock.patch.object(
            finalization,
            "begin",
            side_effect=finalization.FinalizationError("accepted work remains"),
        ):
            with mock.patch("sys.stderr"):
                self.assertEqual(finalization.main(["begin"]), 2)
        history = (paths.logs_dir() / "operations.log").read_text()
        self.assertIn("Finalization begin failed: accepted work remains", history)

    def test_uvicorn_and_package_records_reach_console_and_file_exactly_once(self) -> None:
        environment = dict(os.environ, RUNTIME_ROOT=str(self.root))
        result = subprocess.run(
            [sys.executable, "-c", """
import json, logging, logging.config
from pathlib import Path
logging.config.dictConfig(json.loads(Path('shakemap_service/data/service-logging.json').read_text()))
for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'shakemap_service.main', 'shakemap_service.queue'):
    logging.getLogger(name).warning('unique-' + name)
"""],
            cwd=PROJECT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        contents = (paths.logs_dir() / "service.log").read_text()
        for name in (
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "shakemap_service.main",
            "shakemap_service.queue",
        ):
            marker = f"unique-{name}\n"
            self.assertEqual(contents.count(marker), 1)
            self.assertEqual((result.stdout + result.stderr).count(marker), 1)

    def test_runtime_write_failure_is_visible_on_console(self) -> None:
        handler = service_logging.ServiceLogHandler()
        record = logging.LogRecord("test", 40, "", 0, "runtime error", (), None)
        with mock.patch.object(
            service_logging, "append_record", side_effect=ValueError("disk full")
        ):
            with mock.patch("sys.stderr") as console:
                handler.emit(record)
        self.assertIn(
            "disk full",
            "".join(call.args[0] for call in console.write.call_args_list),
        )

    def _entrypoint(self, body: str) -> tuple[subprocess.Popen[str], Path]:
        binary = self.root / "bin"
        binary.mkdir()
        fake = binary / "uvicorn"
        fake.write_text("#!/usr/bin/env bash\n" + body)
        fake.chmod(0o700)
        pid_file = self.root / "uvicorn.pid"
        environment = dict(
            os.environ,
            RUNTIME_ROOT=str(self.root),
            PATH=str(binary) + os.pathsep + os.environ["PATH"],
            PID_FILE=str(pid_file),
        )
        # The host has no /app mount. Only that final working-directory change
        # is intercepted; the real entrypoint and safe logging module run.
        environment["BASH_FUNC_cd%%"] = '() { if [[ "$1" == /app ]]; then return 0; else builtin cd "$@"; fi; }'
        process = subprocess.Popen(
            ["bash", str(PROJECT / "entrypoint.sh")],
            cwd=PROJECT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process, pid_file

    def test_entrypoint_persists_startup_and_preserves_exec_exit_code(self) -> None:
        process, _ = self._entrypoint("echo 'uvicorn failure' >&2\nexit 23\n")
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 23)
        self.assertIn("uvicorn failure", stderr)
        self.assertIn("Starting shakemap_service", stdout)
        self.assertIn("Starting shakemap_service", (paths.logs_dir() / "service.log").read_text())

    def test_exec_keeps_pid_and_term_delivery(self) -> None:
        process, pid_file = self._entrypoint(
            'echo "$$" > "$PID_FILE"\ntrap "echo stopped; exit 0" TERM\nwhile true; do sleep 0.05; done\n'
        )
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(pid_file.exists())
        self.assertEqual(int(pid_file.read_text()), process.pid)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertIn("stopped", stdout)

    def test_entrypoint_log_failure_stays_visible_and_prevents_api_start(self) -> None:
        paths.logs_dir().mkdir(parents=True)
        destination = self.root / "outside"
        destination.write_text("untouched")
        (paths.logs_dir() / "service.log").symlink_to(destination)
        process, pid_file = self._entrypoint('echo "$$" > "$PID_FILE"\n')
        _, stderr = process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("service.log", stderr)
        self.assertFalse(pid_file.exists())
        self.assertEqual(destination.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
