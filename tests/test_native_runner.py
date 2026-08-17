from __future__ import annotations

import inspect
import signal
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from unittest import mock

from shakemap_service import runner


FIXED_COMMAND = [
    "shake",
    "evt",
    "select",
    "assemble",
    "model",
    "contour",
    "mapping",
    "stations",
    "gridxml",
]


class NativeRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.log_file = Path(self.temporary.name) / "logs" / "shake.log"
        runner.open_launch_gate()

    def tearDown(self) -> None:
        runner.open_launch_gate()
        self.temporary.cleanup()

    def assert_utc_timestamp(self, value: str) -> None:
        self.assertTrue(value.endswith("Z"))
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        self.assertIsNotNone(parsed.tzinfo)

    def test_fixed_command_streams_combined_output_and_preserves_environment(self) -> None:
        process = mock.Mock(pid=4321)
        environment = {"HOME": "/private/profile", "TMPDIR": "/private/tmp"}
        started: list[tuple[int, list[str], str]] = []
        call_order: list[str] = []

        def record_started(pid: int, command: list[str], timestamp: str) -> None:
            call_order.append("on_started")
            started.append((pid, command, timestamp))

        def wait_for_process() -> int:
            call_order.append("wait")
            return 0

        process.wait.side_effect = wait_for_process

        with mock.patch.object(
            runner.subprocess,
            "Popen",
            return_value=process,
        ) as popen:
            result = runner.run_shake(
                "evt",
                log_file=self.log_file,
                env=environment,
                on_started=record_started,
            )

        self.assertEqual(result.command, FIXED_COMMAND)
        self.assertNotIn("modules", inspect.signature(runner.run_shake).parameters)
        self.assertEqual(result.pid, 4321)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.signal)
        self.assert_utc_timestamp(result.started_at)
        self.assert_utc_timestamp(result.completed_at)
        self.assertEqual(started, [(4321, FIXED_COMMAND, result.started_at)])
        self.assertEqual(call_order, ["on_started", "wait"])
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args, (FIXED_COMMAND,))
        self.assertIs(popen.call_args.kwargs["env"], environment)
        self.assertEqual(
            environment,
            {"HOME": "/private/profile", "TMPDIR": "/private/tmp"},
        )
        self.assertIs(
            popen.call_args.kwargs["stderr"],
            runner.subprocess.STDOUT,
        )
        self.assertIsNot(
            popen.call_args.kwargs["stdout"],
            runner.subprocess.PIPE,
        )
        self.assertEqual(
            Path(popen.call_args.kwargs["stdout"].name),
            self.log_file,
        )
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertTrue(self.log_file.is_file())

    def test_terminating_signal_is_distinct_from_exit_code(self) -> None:
        process = mock.Mock(pid=4321)
        process.wait.return_value = -signal.SIGTERM

        with mock.patch.object(runner.subprocess, "Popen", return_value=process):
            result = runner.run_shake(
                "evt",
                log_file=self.log_file,
                env={"HOME": "/private/profile"},
            )

        self.assertIsNone(result.exit_code)
        self.assertEqual(result.signal, signal.SIGTERM)

    def test_normal_nonzero_return_is_preserved_as_exit_code(self) -> None:
        process = mock.Mock(pid=4321)
        process.wait.return_value = 23

        with mock.patch.object(runner.subprocess, "Popen", return_value=process):
            result = runner.run_shake(
                "evt",
                log_file=self.log_file,
                env={"HOME": "/private/profile"},
            )

        self.assertEqual(result.exit_code, 23)
        self.assertIsNone(result.signal)

    def test_invocation_failure_is_propagated(self) -> None:
        failure = FileNotFoundError("shake is unavailable")

        with mock.patch.object(
            runner.subprocess,
            "Popen",
            side_effect=failure,
        ) as popen:
            with self.assertRaises(FileNotFoundError) as caught:
                runner.run_shake(
                    "evt",
                    log_file=self.log_file,
                    env={"HOME": "/private/profile"},
                )

        self.assertIs(caught.exception, failure)
        popen.assert_called_once()

    def test_start_recording_failure_terminates_and_reaps_child(self) -> None:
        process = mock.Mock(pid=4321)
        failure = RuntimeError("start record failed")
        record_start = mock.Mock(side_effect=failure)

        with mock.patch.object(runner.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError) as caught:
                runner.run_shake(
                    "evt",
                    log_file=self.log_file,
                    env={"HOME": "/private/profile"},
                    on_started=record_start,
                )

        self.assertIs(caught.exception, failure)
        record_start.assert_called_once()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with()

    def test_cleanup_errors_do_not_obscure_start_recording_failure(self) -> None:
        process = mock.Mock(pid=4321)
        process.terminate.side_effect = OSError("termination failed")
        process.wait.side_effect = OSError("reaping failed")
        failure = RuntimeError("start record failed")

        with mock.patch.object(runner.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError) as caught:
                runner.run_shake(
                    "evt",
                    log_file=self.log_file,
                    env={"HOME": "/private/profile"},
                    on_started=mock.Mock(side_effect=failure),
                )

        self.assertIs(caught.exception, failure)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with()

    def test_closed_gate_rejects_launch_before_popen(self) -> None:
        runner.close_and_terminate_active()
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaises(runner.ServiceShutdownError):
                runner.run_shake(
                    "evt",
                    log_file=self.log_file,
                    env={"HOME": "/private/profile"},
                )
        popen.assert_not_called()

    def test_shutdown_cannot_miss_a_launch_during_popen(self) -> None:
        popen_entered = Event()
        allow_popen = Event()
        process_released = Event()
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.terminate.side_effect = process_released.set

        def create_process(*args: object, **kwargs: object) -> mock.Mock:
            popen_entered.set()
            self.assertTrue(allow_popen.wait(timeout=5))
            return process

        def wait_for_process() -> int:
            self.assertTrue(process_released.wait(timeout=5))
            return -signal.SIGTERM

        process.wait.side_effect = wait_for_process
        results: list[runner.ExecutionResult] = []
        with mock.patch.object(runner.subprocess, "Popen", side_effect=create_process):
            execution = Thread(
                target=lambda: results.append(
                    runner.run_shake(
                        "evt",
                        log_file=self.log_file,
                        env={"HOME": "/private/profile"},
                    )
                )
            )
            execution.start()
            self.assertTrue(popen_entered.wait(timeout=5))
            terminated: list[int] = []
            shutdown = Thread(
                target=lambda: terminated.append(
                    runner.close_and_terminate_active()
                )
            )
            shutdown.start()
            self.assertTrue(shutdown.is_alive())
            allow_popen.set()
            shutdown.join(timeout=5)
            execution.join(timeout=5)

        self.assertFalse(shutdown.is_alive())
        self.assertFalse(execution.is_alive())
        self.assertEqual(terminated, [1])
        self.assertTrue(results[0].service_terminated)
        self.assertEqual(results[0].signal, signal.SIGTERM)

    def test_force_kills_a_stubborn_tracked_process(self) -> None:
        waiting = Event()
        killed = Event()
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        process.kill.side_effect = killed.set

        def wait_for_process() -> int:
            waiting.set()
            self.assertTrue(killed.wait(timeout=5))
            return -signal.SIGKILL

        process.wait.side_effect = wait_for_process
        results: list[runner.ExecutionResult] = []
        with mock.patch.object(runner.subprocess, "Popen", return_value=process):
            execution = Thread(
                target=lambda: results.append(
                    runner.run_shake(
                        "evt",
                        log_file=self.log_file,
                        env={"HOME": "/private/profile"},
                    )
                )
            )
            execution.start()
            self.assertTrue(waiting.wait(timeout=5))
            self.assertEqual(runner.close_and_terminate_active(), 1)
            self.assertEqual(runner.force_kill_active(), 1)
            execution.join(timeout=5)

        self.assertFalse(execution.is_alive())
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertTrue(results[0].service_terminated)

    def test_wait_failure_keeps_the_unresolved_process_owned(self) -> None:
        process = mock.Mock(pid=4321)
        process.wait.side_effect = OSError("wait failed")
        process.poll.return_value = None
        with mock.patch.object(runner.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(OSError, "wait failed"):
                runner.run_shake(
                    "evt",
                    log_file=self.log_file,
                    env={"HOME": "/private/profile"},
                )
        self.assertEqual(runner.close_and_terminate_active(), 1)
        process.terminate.assert_called_once_with()
        process.poll.return_value = 0
        runner.open_launch_gate()

    def test_signal_failures_keep_ownership_and_counts_truthful(self) -> None:
        process = mock.Mock(pid=4321)
        process.wait.side_effect = OSError("wait failed")
        process.poll.return_value = None
        process.terminate.side_effect = OSError("terminate failed")
        with mock.patch.object(runner.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(OSError, "wait failed"):
                runner.run_shake(
                    "evt",
                    log_file=self.log_file,
                    env={"HOME": "/private/profile"},
                )

        self.assertEqual(runner.close_and_terminate_active(), 0)
        with self.assertRaises(runner.ServiceShutdownError):
            runner.open_launch_gate()
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaises(runner.ServiceShutdownError):
                runner.run_shake(
                    "later",
                    log_file=self.log_file,
                    env={"HOME": "/private/profile"},
                )
        popen.assert_not_called()
        self.assertEqual(runner.force_kill_active(), 1)
        process.kill.assert_called_once_with()
        process.poll.return_value = 0
        runner.open_launch_gate()

    def test_process_control_baseexceptions_propagate(self) -> None:
        for operation, error in (
            ("poll", KeyboardInterrupt()),
            ("terminate", KeyboardInterrupt()),
            ("kill", SystemExit()),
        ):
            with self.subTest(operation=operation):
                process = mock.Mock(pid=4321)
                process.wait.side_effect = OSError("wait failed")
                process.poll.return_value = None
                with mock.patch.object(
                    runner.subprocess,
                    "Popen",
                    return_value=process,
                ):
                    with self.assertRaisesRegex(OSError, "wait failed"):
                        runner.run_shake(
                            "evt",
                            log_file=self.log_file,
                            env={"HOME": "/private/profile"},
                        )

                if operation == "poll":
                    process.poll.side_effect = error
                    invoke = runner.close_and_terminate_active
                elif operation == "terminate":
                    process.terminate.side_effect = error
                    invoke = runner.close_and_terminate_active
                else:
                    process.terminate.side_effect = OSError("terminate failed")
                    self.assertEqual(runner.close_and_terminate_active(), 0)
                    process.poll.side_effect = None
                    process.poll.return_value = None
                    process.kill.side_effect = error
                    invoke = runner.force_kill_active

                with self.assertRaises(type(error)):
                    invoke()
                process.poll.side_effect = None
                process.poll.return_value = 0
                process.terminate.side_effect = None
                process.kill.side_effect = None
                runner.open_launch_gate()


if __name__ == "__main__":
    unittest.main()
