#!/usr/bin/env python3
"""Host tests for readiness-gated admission and application shutdown."""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from threading import Event, Lock, Thread
from unittest import mock

from shakemap_service import main, paths, readiness, runner, status, worker
from shakemap_service.config import Settings
from shakemap_service.scheduler import Scheduler
from shakemap_service.submission import Upload, accept_request


class ApplicationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="application_lifecycle_")
        self.previous_paths_settings = paths.settings
        self.previous_status_settings = status.settings
        self.previous_restart_required = (
            main._restart_required_after_incomplete_callback_drain
        )
        main._restart_required_after_incomplete_callback_drain = False
        self.configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
            max_concurrent=2,
        )
        paths.settings = self.configured
        status.settings = self.configured
        runner.open_launch_gate()

    def tearDown(self) -> None:
        main._restart_required_after_incomplete_callback_drain = (
            self.previous_restart_required
        )
        runner.open_launch_gate()
        status.settings = self.previous_status_settings
        paths.settings = self.previous_paths_settings
        self.temporary.cleanup()

    def _accept(self, event_id: str):
        return accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(event_id.encode("utf-8")))],
        )

    @staticmethod
    def _snapshot(directory: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }

    def test_not_ready_and_mismatch_leave_queued_work_unchanged(self) -> None:
        accepted = self._accept("waiting")
        queued = paths.queue_entry_dir(accepted.internal_sequence)
        before = self._snapshot(queued)
        stopping = asyncio.Event()
        states = [
            {"ready": False, "reason": readiness.NOT_RECORDED},
            {"ready": False, "reason": readiness.MISMATCH},
            {"ready": False, "reason": readiness.UNAVAILABLE},
        ]

        def read_state() -> dict[str, object]:
            state = states.pop(0)
            if not states:
                stopping.set()
            return state

        scheduler = mock.Mock()
        with (
            mock.patch.object(main.readiness, "read_readiness", side_effect=read_state),
            mock.patch.object(main, "ADMISSION_INTERVAL_SECONDS", 0.001),
            mock.patch.object(main.logger, "error") as logged,
        ):
            asyncio.run(main._admit_ready_work(scheduler, stopping))

        scheduler.tick.assert_not_called()
        logged.assert_called_once_with(
            "Calculation admission readiness is unavailable"
        )
        self.assertEqual(self._snapshot(queued), before)
        self.assertEqual(status.read_status(accepted.internal_sequence).status, "QUEUED")

    def test_matching_readiness_uses_existing_oldest_eligible_scheduler(self) -> None:
        first = self._accept("same")
        blocked = self._accept("same")
        different = self._accept("different")
        stopping = asyncio.Event()
        called: list[int] = []
        called_lock = Lock()

        def complete(record: status.CalculationRecord) -> None:
            with called_lock:
                called.append(record.internal_sequence)
            status.transition_to_failed(
                record.internal_sequence,
                "fixture completion",
                code="fixture_completion",
            )

        scheduler = Scheduler(complete, service_settings=self.configured)

        def ready_once() -> dict[str, object]:
            stopping.set()
            return {"ready": True, "reason": None}

        try:
            with mock.patch.object(
                main.readiness,
                "read_readiness",
                side_effect=ready_once,
            ):
                asyncio.run(main._admit_ready_work(scheduler, stopping))
            self.assertTrue(scheduler.wait_until_idle(timeout=5))
        finally:
            scheduler.shutdown()

        self.assertEqual(set(called), {first.internal_sequence, different.internal_sequence})
        self.assertEqual(status.read_status(blocked.internal_sequence).status, "QUEUED")

    def test_readiness_turning_false_blocks_the_next_promotion(self) -> None:
        stopping = asyncio.Event()
        reads = 0

        def read_state() -> dict[str, object]:
            nonlocal reads
            reads += 1
            if reads == 1:
                return {"ready": True, "reason": None}
            stopping.set()
            return {"ready": False, "reason": readiness.MISMATCH}

        scheduler = mock.Mock()
        with (
            mock.patch.object(main.readiness, "read_readiness", side_effect=read_state),
            mock.patch.object(main, "ADMISSION_INTERVAL_SECONDS", 0.001),
        ):
            asyncio.run(main._admit_ready_work(scheduler, stopping))

        scheduler.tick.assert_called_once_with()

    def test_admission_loop_survives_read_and_tick_errors_with_waits(self) -> None:
        stopping = asyncio.Event()
        read_calls = 0
        tick_calls = 0

        def read_state() -> dict[str, object]:
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                raise OSError("read failed")
            return {"ready": True, "reason": None}

        def tick() -> None:
            nonlocal tick_calls
            tick_calls += 1
            if tick_calls == 1:
                raise RuntimeError("tick failed")
            stopping.set()

        scheduler = mock.Mock()
        scheduler.tick.side_effect = tick
        real_wait_for = asyncio.wait_for
        with (
            mock.patch.object(main.readiness, "read_readiness", side_effect=read_state),
            mock.patch.object(main, "ADMISSION_INTERVAL_SECONDS", 0.001),
            mock.patch.object(main.logger, "exception") as logged,
            mock.patch.object(main.asyncio, "wait_for", wraps=real_wait_for) as waited,
        ):
            asyncio.run(main._admit_ready_work(scheduler, stopping))

        self.assertEqual(read_calls, 3)
        self.assertEqual(tick_calls, 2)
        self.assertEqual(logged.call_count, 2)
        self.assertEqual(waited.call_count, 3)

    def test_recovery_precedes_scheduler_ownership_and_admission(self) -> None:
        order: list[object] = []
        ticked = asyncio.Event()

        class FakeScheduler:
            def __init__(self, callback: object) -> None:
                order.append(("scheduler", callback))

            def tick(self) -> None:
                order.append("tick")
                ticked.set()

            def shutdown(self, *, wait: bool) -> None:
                order.append(("shutdown", wait))

            def wait_until_idle(self, timeout: float) -> bool:
                order.append(("wait", timeout))
                return True

        async def exercise() -> None:
            async with main.lifespan(main.app):
                await asyncio.wait_for(ticked.wait(), timeout=2)

        with (
            mock.patch.object(
                main.startup_recovery,
                "recover_interrupted_calculations",
                side_effect=lambda: order.append("recovery") or (),
            ),
            mock.patch.object(main, "Scheduler", FakeScheduler),
            mock.patch.object(
                main.readiness,
                "read_readiness",
                return_value={"ready": True, "reason": None},
            ),
            mock.patch.object(
                main.runner,
                "open_launch_gate",
                side_effect=lambda: order.append("open"),
            ),
            mock.patch.object(
                main.runner,
                "close_and_terminate_active",
                side_effect=lambda: order.append("close") or 0,
            ),
            mock.patch.object(main.runner, "force_kill_active") as killed,
        ):
            asyncio.run(exercise())

        scheduler_created = order.index(("scheduler", worker.execute_shakemap))
        self.assertLess(order.index("recovery"), order.index("open"))
        self.assertLess(order.index("open"), scheduler_created)
        self.assertLess(scheduler_created, order.index("tick"))
        self.assertLess(order.index(("shutdown", False)), order.index(("wait", 60.0)))
        self.assertLess(order.index(("wait", 60.0)), order.index("close"))
        killed.assert_called_once_with()

    def test_recovery_failure_prevents_scheduler_and_native_ownership(self) -> None:
        failure = RuntimeError("recovery failed")

        async def exercise() -> None:
            async with main.lifespan(main.app):
                self.fail("failed recovery must not enter the application lifespan")

        with (
            mock.patch.object(
                main.startup_recovery,
                "recover_interrupted_calculations",
                side_effect=failure,
            ),
            mock.patch.object(main, "Scheduler") as scheduler,
            mock.patch.object(main.runner, "open_launch_gate") as opened,
            mock.patch.object(worker, "execute_shakemap") as native,
        ):
            with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                asyncio.run(exercise())
        scheduler.assert_not_called()
        opened.assert_not_called()
        native.assert_not_called()

    def test_idle_shutdown_performs_no_live_process_action(self) -> None:
        stopping = asyncio.Event()
        scheduler = mock.Mock()
        scheduler.wait_until_idle.return_value = True

        async def admission() -> None:
            await stopping.wait()

        async def exercise() -> None:
            task = asyncio.create_task(admission())
            await main._shutdown_scheduler(scheduler, stopping, task)

        with (
            mock.patch.object(
                main.runner,
                "close_and_terminate_active",
                return_value=0,
            ) as closed,
            mock.patch.object(main.runner, "force_kill_active") as killed,
        ):
            asyncio.run(exercise())

        scheduler.shutdown.assert_called_once_with(wait=False)
        self.assertEqual(
            scheduler.wait_until_idle.call_args_list,
            [mock.call(60.0), mock.call(2.0), mock.call(2.0)],
        )
        closed.assert_called_once_with()
        killed.assert_called_once_with()

    def test_shutdown_closes_scheduler_before_stop_is_visible_to_tick(self) -> None:
        stopping = asyncio.Event()
        lock = Lock()
        tick_entered = Event()
        shutdown_entered = Event()
        release_tick = Event()
        promoted_after_stop: list[bool] = []

        class LockedScheduler:
            def tick(self) -> None:
                with lock:
                    tick_entered.set()
                    if not release_tick.wait(timeout=5):
                        raise AssertionError("tick was not released")
                    promoted_after_stop.append(stopping.is_set())

            def shutdown(self, *, wait: bool) -> None:
                shutdown_entered.set()
                with lock:
                    pass

            def wait_until_idle(self, timeout: float) -> bool:
                return True

        scheduler = LockedScheduler()

        def release_during_shutdown() -> None:
            if not shutdown_entered.wait(timeout=5):
                raise AssertionError("shutdown did not begin")
            release_tick.set()

        async def exercise() -> None:
            admission = asyncio.create_task(
                main._admit_ready_work(scheduler, stopping)
            )
            self.assertTrue(await asyncio.to_thread(tick_entered.wait, 5))
            releaser = Thread(target=release_during_shutdown)
            releaser.start()
            await main._shutdown_scheduler(scheduler, stopping, admission)
            releaser.join(timeout=5)
            self.assertFalse(releaser.is_alive())

        with (
            mock.patch.object(
                main.readiness,
                "read_readiness",
                return_value={"ready": True, "reason": None},
            ),
            mock.patch.object(main.runner, "close_and_terminate_active", return_value=0),
            mock.patch.object(main.runner, "force_kill_active", return_value=0),
        ):
            asyncio.run(exercise())

        self.assertEqual(promoted_after_stop, [False])

    def test_admission_baseexceptions_do_not_skip_cleanup(self) -> None:
        class AdmissionAbort(BaseException):
            pass

        for mode, expected in (
            ("cancel", asyncio.CancelledError),
            ("abort", AdmissionAbort),
        ):
            with self.subTest(mode=mode):
                stopping = asyncio.Event()
                scheduler = mock.Mock()
                first_drain = (
                    RuntimeError("drain failed") if mode == "abort" else False
                )
                scheduler.wait_until_idle.side_effect = (
                    first_drain,
                    False,
                    True,
                )

                async def admission() -> None:
                    if mode == "abort":
                        raise AdmissionAbort()
                    await asyncio.Event().wait()

                async def exercise() -> None:
                    task = asyncio.create_task(admission())
                    if mode == "cancel":
                        task.cancel()
                    await main._shutdown_scheduler(scheduler, stopping, task)

                with (
                    mock.patch.object(
                        main.runner,
                        "close_and_terminate_active",
                        return_value=1,
                    ) as closed,
                    mock.patch.object(
                        main.runner,
                        "force_kill_active",
                        return_value=1,
                    ) as killed,
                ):
                    with self.assertRaises(expected):
                        asyncio.run(exercise())

                scheduler.shutdown.assert_called_once_with(wait=False)
                self.assertEqual(
                    scheduler.wait_until_idle.call_args_list,
                    [mock.call(60.0), mock.call(2.0), mock.call(2.0)],
                )
                closed.assert_called_once_with()
                killed.assert_called_once_with()

    def test_initial_drain_error_does_not_skip_later_cleanup(self) -> None:
        class InitialDrainAbort(BaseException):
            pass

        failure = InitialDrainAbort()
        stopping = asyncio.Event()
        scheduler = mock.Mock()
        scheduler.wait_until_idle.side_effect = (failure, False, True)

        async def admission() -> None:
            await stopping.wait()

        async def exercise() -> None:
            task = asyncio.create_task(admission())
            await main._shutdown_scheduler(scheduler, stopping, task)

        with (
            mock.patch.object(
                main.runner,
                "close_and_terminate_active",
                return_value=1,
            ) as closed,
            mock.patch.object(
                main.runner,
                "force_kill_active",
                return_value=1,
            ) as killed,
        ):
            with self.assertRaises(InitialDrainAbort) as caught:
                asyncio.run(exercise())

        self.assertIs(caught.exception, failure)
        self.assertEqual(
            scheduler.wait_until_idle.call_args_list,
            [mock.call(60.0), mock.call(2.0), mock.call(2.0)],
        )
        closed.assert_called_once_with()
        killed.assert_called_once_with()
        self.assertFalse(main._restart_required_after_incomplete_callback_drain)

    def test_incomplete_final_drain_blocks_another_lifespan(self) -> None:
        final_failure = RuntimeError("final drain failed")
        for final_result in (False, final_failure):
            with self.subTest(final_result=final_result):
                main._restart_required_after_incomplete_callback_drain = False
                stopping = asyncio.Event()
                scheduler = mock.Mock()
                scheduler.wait_until_idle.side_effect = (
                    False,
                    False,
                    final_result,
                )

                async def admission() -> None:
                    await stopping.wait()

                async def shutdown() -> None:
                    task = asyncio.create_task(admission())
                    await main._shutdown_scheduler(scheduler, stopping, task)

                with (
                    mock.patch.object(
                        main.runner,
                        "close_and_terminate_active",
                        return_value=1,
                    ),
                    mock.patch.object(
                        main.runner,
                        "force_kill_active",
                        return_value=1,
                    ),
                ):
                    if isinstance(final_result, BaseException):
                        with self.assertRaises(RuntimeError) as caught:
                            asyncio.run(shutdown())
                        self.assertIs(caught.exception, final_failure)
                    else:
                        asyncio.run(shutdown())

                self.assertTrue(
                    main._restart_required_after_incomplete_callback_drain
                )

                async def restart() -> None:
                    async with main.lifespan(main.app):
                        self.fail("an incomplete callback drain requires restart")

                with (
                    mock.patch.object(
                        main.startup_recovery,
                        "recover_interrupted_calculations",
                    ) as recovered,
                    mock.patch.object(main.runner, "open_launch_gate") as opened,
                    mock.patch.object(main, "Scheduler") as scheduler_type,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "process restart is required",
                    ):
                        asyncio.run(restart())

                recovered.assert_not_called()
                opened.assert_not_called()
                scheduler_type.assert_not_called()

    def test_fully_drained_shutdown_allows_repeated_lifespans(self) -> None:
        schedulers: list[object] = []

        class FullyDrainedScheduler:
            def __init__(self, callback: object) -> None:
                schedulers.append(callback)

            def tick(self) -> None:
                raise AssertionError("not-ready admission must not tick")

            def shutdown(self, *, wait: bool) -> None:
                self.closed = not wait

            def wait_until_idle(self, timeout: float) -> bool:
                return True

        async def exercise() -> None:
            for _ in range(2):
                async with main.lifespan(main.app):
                    await asyncio.sleep(0)

        with (
            mock.patch.object(
                main.startup_recovery,
                "recover_interrupted_calculations",
                return_value=(),
            ) as recovered,
            mock.patch.object(main, "Scheduler", FullyDrainedScheduler),
            mock.patch.object(
                main.readiness,
                "read_readiness",
                return_value={"ready": False, "reason": readiness.NOT_RECORDED},
            ),
            mock.patch.object(main.runner, "open_launch_gate") as opened,
            mock.patch.object(
                main.runner,
                "close_and_terminate_active",
                return_value=0,
            ),
            mock.patch.object(main.runner, "force_kill_active", return_value=0),
        ):
            asyncio.run(exercise())

        self.assertEqual(schedulers, [worker.execute_shakemap, worker.execute_shakemap])
        self.assertEqual(recovered.call_count, 2)
        self.assertEqual(opened.call_count, 2)
        self.assertFalse(main._restart_required_after_incomplete_callback_drain)

    def test_unresolved_prior_child_refuses_restart_before_scheduler_creation(self) -> None:
        process = mock.Mock(pid=4321)
        process.wait.side_effect = OSError("wait failed")
        process.poll.return_value = None
        with mock.patch.object(runner.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(OSError, "wait failed"):
                runner.run_shake(
                    "evt",
                    log_file=Path(self.temporary.name) / "shake.log",
                    env={"HOME": "/private/profile"},
                )

        async def exercise() -> None:
            async with main.lifespan(main.app):
                self.fail("unresolved process ownership must prevent startup")

        with (
            mock.patch.object(
                main.startup_recovery,
                "recover_interrupted_calculations",
                return_value=(),
            ) as recovered,
            mock.patch.object(main, "Scheduler") as scheduler,
        ):
            with self.assertRaises(runner.ServiceShutdownError):
                asyncio.run(exercise())

        recovered.assert_called_once_with()
        scheduler.assert_not_called()
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaises(runner.ServiceShutdownError):
                runner.run_shake(
                    "later",
                    log_file=Path(self.temporary.name) / "later.log",
                    env={"HOME": "/private/profile"},
                )
        popen.assert_not_called()
        process.poll.return_value = 0
        runner.open_launch_gate()

    def test_active_shutdown_closes_admission_then_terminates_and_kills(self) -> None:
        queued = self._accept("still-queued")
        queued_path = paths.queue_entry_dir(queued.internal_sequence)
        before = self._snapshot(queued_path)
        order: list[object] = []
        stopping = asyncio.Event()

        class FakeScheduler:
            def __init__(self) -> None:
                self.wait_results = iter((False, False, True))

            def shutdown(self, *, wait: bool) -> None:
                order.append(("shutdown", wait))

            def wait_until_idle(self, timeout: float) -> bool:
                order.append(("wait", timeout))
                return next(self.wait_results)

        async def admission() -> None:
            await stopping.wait()
            order.append("admission_stopped")

        async def exercise() -> None:
            task = asyncio.create_task(admission())
            await main._shutdown_scheduler(FakeScheduler(), stopping, task)

        with (
            mock.patch.object(
                main.runner,
                "close_and_terminate_active",
                side_effect=lambda: order.append("terminate") or 1,
            ),
            mock.patch.object(
                main.runner,
                "force_kill_active",
                side_effect=lambda: order.append("kill") or 1,
            ),
        ):
            asyncio.run(exercise())

        self.assertEqual(
            order,
            [
                ("shutdown", False),
                "admission_stopped",
                ("wait", 60.0),
                "terminate",
                ("wait", 2.0),
                "kill",
                ("wait", 2.0),
            ],
        )
        self.assertEqual(self._snapshot(queued_path), before)
        self.assertEqual(status.read_status(queued.internal_sequence).status, "QUEUED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
