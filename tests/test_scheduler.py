#!/usr/bin/env python3
"""Host tests for bounded queue admission and event reservations."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest import mock

from shakemap_service import paths, recalculation, status
from shakemap_service.config import Settings
from shakemap_service.scheduler import Scheduler
from shakemap_service.submission import Upload, accept_request


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_path_settings = paths.settings
        self.previous_status_settings = status.settings
        self.configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
            max_concurrent=2,
        )
        paths.settings = self.configured
        status.settings = self.configured
        self.schedulers: list[Scheduler] = []

    def tearDown(self) -> None:
        for scheduler in self.schedulers:
            scheduler.shutdown()
        paths.settings = self.previous_path_settings
        status.settings = self.previous_status_settings
        self.temporary.cleanup()

    def _accept(self, event_id: str):
        return accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(event_id.encode("utf-8")))],
        )

    def _scheduler(self, callback, *, capacity: int = 2) -> Scheduler:
        scheduler = Scheduler(
            callback,
            service_settings=Settings(
                runtime_root=self.temporary.name,
                shared_runtime_root="/operator/runtime",
                max_concurrent=capacity,
            ),
        )
        self.schedulers.append(scheduler)
        return scheduler

    def _wait_for_errors(self, scheduler: Scheduler, count: int) -> None:
        deadline = time.monotonic() + 5
        while len(scheduler.errors) < count:
            if time.monotonic() >= deadline:
                self.fail("scheduler error was not recorded")
            Event().wait(0.01)

    @staticmethod
    def _complete_failed(record: status.CalculationRecord) -> None:
        status.transition_status(
            record.internal_sequence,
            status.LifecycleState.FAILED,
            failure={"code": "test_completion", "message": "fixture"},
            service_outcome={"completed": True, "successful": False},
        )

    def _promote_failed_current(self, event_id: str) -> status.CalculationRecord:
        accepted = self._accept(event_id)
        status.transition_to_running(accepted.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(accepted.internal_sequence).rename(
            paths.event_service_dir(event_id)
        )
        return status.transition_current_record(
            event_id,
            status.LifecycleState.FAILED,
            failure={"code": "test_failure", "message": "fixture"},
            service_outcome={"completed": True, "successful": False},
        )

    def test_capacity_and_default_setting_bound_concurrent_callbacks(self) -> None:
        accepted = [self._accept(name) for name in ("one", "two", "three")]
        release = Event()
        entered = Event()
        running = 0
        peak = 0
        lock = Lock()

        def callback(record: status.CalculationRecord) -> None:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
                if running == 2:
                    entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("callbacks were not released")
            self._complete_failed(record)
            with lock:
                running -= 1

        scheduler = self._scheduler(callback)
        self.assertEqual(scheduler.max_concurrent, self.configured.max_concurrent)
        started = scheduler.tick()
        try:
            self.assertTrue(entered.wait(timeout=5))
            self.assertEqual(
                [record.internal_sequence for record in started],
                [accepted[0].internal_sequence, accepted[1].internal_sequence],
            )
            self.assertEqual(scheduler.active_count, 2)
            self.assertEqual(
                status.read_status(accepted[2].internal_sequence).status,
                "QUEUED",
            )
            self.assertEqual(peak, 2)
        finally:
            release.set()
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

        later = scheduler.tick()
        self.assertEqual(
            [record.internal_sequence for record in later],
            [accepted[2].internal_sequence],
        )
        self.assertTrue(scheduler.wait_until_idle(timeout=5))
        self.assertEqual(peak, 2)

    def test_same_event_is_serialized_without_blocking_a_different_event(self) -> None:
        first = self._accept("same")
        second = self._accept("same")
        different = self._accept("different")
        release_first = Event()
        release_different = Event()
        entered = {
            first.internal_sequence: Event(),
            different.internal_sequence: Event(),
        }

        def callback(record: status.CalculationRecord) -> None:
            if record.internal_sequence == first.internal_sequence:
                entered[first.internal_sequence].set()
                release = release_first
            elif record.internal_sequence == different.internal_sequence:
                entered[different.internal_sequence].set()
                release = release_different
            else:
                release = Event()
                release.set()
            if not release.wait(timeout=5):
                raise AssertionError("callback was not released")
            self._complete_failed(record)

        scheduler = self._scheduler(callback)
        started = scheduler.tick()
        try:
            self.assertTrue(entered[first.internal_sequence].wait(timeout=5))
            self.assertTrue(entered[different.internal_sequence].wait(timeout=5))
            self.assertEqual(
                [record.internal_sequence for record in started],
                [first.internal_sequence, different.internal_sequence],
            )
            self.assertEqual(
                status.read_status(second.internal_sequence).status,
                "QUEUED",
            )
            self.assertEqual(scheduler.reserved_event_ids, {"same", "different"})
        finally:
            release_first.set()
            release_different.set()
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

        self.assertEqual(
            [record.internal_sequence for record in scheduler.tick()],
            [second.internal_sequence],
        )
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

    def test_concurrent_ticks_do_not_double_claim_a_sequence_or_event(self) -> None:
        first = self._accept("same")
        second = self._accept("same")
        release = Event()
        entered = Event()
        calls: list[int] = []
        lock = Lock()

        def callback(record: status.CalculationRecord) -> None:
            with lock:
                calls.append(record.internal_sequence)
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("callback was not released")
            self._complete_failed(record)

        scheduler = self._scheduler(callback)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                ticks = [executor.submit(scheduler.tick) for _ in range(2)]
                results = [future.result(timeout=5) for future in ticks]
            self.assertTrue(entered.wait(timeout=5))
            self.assertEqual(sum(len(result) for result in results), 1)
            self.assertEqual(calls, [first.internal_sequence])
            self.assertEqual(
                status.read_status(second.internal_sequence).status,
                "QUEUED",
            )
        finally:
            release.set()
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

    def test_numeric_malformed_entry_blocks_only_later_sequences(self) -> None:
        before = self._accept("before")
        malformed = self._accept("untrusted")
        after = self._accept("after")
        paths.queue_status_file(malformed.internal_sequence).write_text(
            "{broken",
            encoding="utf-8",
        )
        release = Event()

        def callback(record: status.CalculationRecord) -> None:
            if not release.wait(timeout=5):
                raise AssertionError("callback was not released")
            self._complete_failed(record)

        scheduler = self._scheduler(callback)
        started = scheduler.tick()
        try:
            self.assertEqual(
                [record.internal_sequence for record in started],
                [before.internal_sequence],
            )
            self.assertEqual(
                status.read_status(after.internal_sequence).status,
                "QUEUED",
            )
        finally:
            release.set()
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

    def test_nonnumeric_and_hidden_debris_do_not_block_valid_work(self) -> None:
        accepted = self._accept("valid")
        paths.queue_dir().joinpath("visible-debris").mkdir()
        paths.queue_dir().joinpath(".hidden-debris").mkdir()
        scheduler = self._scheduler(self._complete_failed, capacity=1)

        started = scheduler.tick()

        self.assertEqual(
            [record.internal_sequence for record in started],
            [accepted.internal_sequence],
        )
        self.assertTrue(scheduler.wait_until_idle(timeout=5))
        self.assertEqual(
            status.read_status(accepted.internal_sequence).status,
            "FAILED",
        )

    def test_callback_failures_do_not_stop_unrelated_work(self) -> None:
        returned = self._accept("returned")
        raised = self._accept("raised")
        succeeded = self._accept("succeeded")

        def callback(record: status.CalculationRecord) -> None:
            if record.internal_sequence == raised.internal_sequence:
                raise RuntimeError("injected callback failure")
            if record.internal_sequence == succeeded.internal_sequence:
                self._complete_failed(record)

        scheduler = self._scheduler(callback)

        with mock.patch.object(
            recalculation,
            "reconcile_transaction",
            wraps=recalculation.reconcile_transaction,
        ) as reconcile:
            self.assertEqual(len(scheduler.tick()), 2)
            self.assertTrue(scheduler.wait_until_idle(timeout=5))
        reconcile.assert_called_once_with("raised", raised.internal_sequence)

        returned_record = status.read_status(returned.internal_sequence)
        raised_record = status.read_status(raised.internal_sequence)
        self.assertEqual(returned_record.status, "FAILED")
        self.assertEqual(
            returned_record.failure["code"],
            "scheduler_callback_incomplete",
        )
        self.assertEqual(raised_record.status, "FAILED")
        self.assertEqual(raised_record.failure["code"], "scheduler_callback_failed")
        self.assertIn("injected callback failure", raised_record.failure["message"])
        self.assertEqual(scheduler.active_count, 0)
        self.assertEqual(scheduler.errors, ())

        self.assertEqual(
            [record.internal_sequence for record in scheduler.tick()],
            [succeeded.internal_sequence],
        )
        self.assertTrue(scheduler.wait_until_idle(timeout=5))
        self.assertEqual(
            status.read_status(succeeded.internal_sequence).status,
            "FAILED",
        )

    def test_failed_running_transition_releases_the_unstarted_reservation(self) -> None:
        accepted = self._accept("not-started")
        scheduler = self._scheduler(self._complete_failed, capacity=1)

        with mock.patch.object(
            status,
            "transition_to_running",
            side_effect=OSError("injected running transition failure"),
        ):
            self.assertEqual(scheduler.tick(), ())

        self.assertEqual(
            status.read_status(accepted.internal_sequence).status,
            "QUEUED",
        )
        self.assertEqual(scheduler.active_count, 0)
        self.assertEqual(scheduler.reserved_event_ids, frozenset())
        self.assertEqual(len(scheduler.errors), 1)
        self.assertIn(
            "injected running transition failure",
            scheduler.errors[0].message,
        )

    def test_reservation_is_retained_when_terminal_write_fails(self) -> None:
        accepted = self._accept("retained")
        scheduler = self._scheduler(lambda record: None, capacity=1)

        with mock.patch.object(
            status,
            "transition_to_failed",
            side_effect=OSError("injected terminal write failure"),
        ):
            self.assertEqual(len(scheduler.tick()), 1)
            self.assertFalse(scheduler.wait_until_idle(timeout=0.1))

        self.assertEqual(scheduler.active_count, 1)
        self.assertEqual(scheduler.reserved_event_ids, {"retained"})
        self.assertEqual(
            status.read_status(accepted.internal_sequence).status,
            "RUNNING",
        )
        self.assertEqual(len(scheduler.errors), 1)
        self.assertIn("injected terminal write failure", scheduler.errors[0].message)

    def test_reconciliation_failure_retains_only_the_affected_event(self) -> None:
        interrupted = self._accept("same")

        def callback(record: status.CalculationRecord) -> None:
            if record.event_id != "same":
                self._complete_failed(record)
                return

            def interrupt(position: str, kind: str, name: str) -> None:
                if (position, kind, name) == (
                    "after",
                    "checkpoint",
                    "products_staged",
                ):
                    raise RuntimeError("injected preparation interruption")

            with mock.patch.object(
                recalculation,
                "_operation_boundary",
                side_effect=interrupt,
            ):
                try:
                    recalculation.prepare_calculation(record.internal_sequence)
                except RuntimeError:
                    stage = (
                        paths.queue_entry_dir(record.internal_sequence)
                        / recalculation.STAGING_DIRECTORY
                    )
                    (stage / "products").mkdir()
                    raise

        scheduler = self._scheduler(callback)
        with mock.patch.object(
            recalculation,
            "reconcile_transaction",
            wraps=recalculation.reconcile_transaction,
        ) as reconcile:
            self.assertEqual(len(scheduler.tick()), 1)
            self._wait_for_errors(scheduler, 1)

        reconcile.assert_called_once_with("same", interrupted.internal_sequence)
        self.assertTrue(
            (
                paths.queue_entry_dir(interrupted.internal_sequence)
                / recalculation.TRANSACTION_FILE
            ).is_file()
        )
        self.assertTrue(
            (
                paths.queue_entry_dir(interrupted.internal_sequence)
                / recalculation.STAGING_DIRECTORY
            ).is_dir()
        )
        self.assertEqual(
            status.read_status(interrupted.internal_sequence).status,
            "RUNNING",
        )
        journal = json.loads(
            (
                paths.queue_entry_dir(interrupted.internal_sequence)
                / recalculation.TRANSACTION_FILE
            ).read_text(encoding="utf-8")
        )
        self.assertIsNotNone(journal["recovery_failure"])
        self.assertEqual(scheduler.active_count, 1)
        self.assertFalse(scheduler.wait_until_idle(timeout=0.1))
        self.assertEqual(scheduler.reserved_event_ids, {"same"})

        later_same = self._accept("same")
        different = self._accept("different")
        started = scheduler.tick()
        scheduler.shutdown()

        self.assertEqual(
            [record.internal_sequence for record in started],
            [different.internal_sequence],
        )
        self.assertEqual(
            status.read_status(later_same.internal_sequence).status,
            "QUEUED",
        )
        self.assertEqual(
            status.read_status(different.internal_sequence).status,
            "FAILED",
        )
        self.assertEqual(scheduler.reserved_event_ids, {"same"})

    def test_prepared_current_record_is_failed_finalized_and_released(self) -> None:
        accepted = self._accept("prepared")

        def prepare_then_return(record: status.CalculationRecord) -> None:
            recalculation.prepare_calculation(record.internal_sequence)

        scheduler = self._scheduler(prepare_then_return, capacity=1)

        self.assertEqual(len(scheduler.tick()), 1)
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

        self.assertIsNone(status.read_status(accepted.internal_sequence))
        current = status.read_current_record("prepared")
        self.assertEqual(current.status, "FAILED")
        self.assertEqual(
            current.failure["code"],
            "scheduler_callback_incomplete",
        )
        self.assertFalse(
            (
                paths.event_service_dir("prepared")
                / recalculation.TRANSACTION_FILE
            ).exists()
        )
        self.assertEqual(scheduler.active_count, 0)

    def test_malformed_journal_retains_the_current_event_reservation(self) -> None:
        accepted = self._accept("unfinalized")

        def prepare_then_return(record: status.CalculationRecord) -> None:
            if record.event_id != "unfinalized":
                self._complete_failed(record)
                return
            recalculation.prepare_calculation(record.internal_sequence)
            (
                paths.event_service_dir(record.event_id)
                / recalculation.TRANSACTION_FILE
            ).write_text("{broken", encoding="utf-8")

        scheduler = self._scheduler(prepare_then_return, capacity=1)
        self.assertEqual(len(scheduler.tick()), 1)
        self._wait_for_errors(scheduler, 1)
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

        current = status.read_current_record("unfinalized")
        self.assertEqual(current.internal_sequence, accepted.internal_sequence)
        self.assertEqual(current.status, "FAILED")
        self.assertEqual(scheduler.active_count, 0)
        self.assertEqual(scheduler.reserved_event_ids, {"unfinalized"})
        self.assertTrue(
            (
                paths.event_service_dir("unfinalized")
                / recalculation.TRANSACTION_FILE
            ).is_file()
        )

        later_same = self._accept("unfinalized")
        different = self._accept("different")
        started = scheduler.tick()
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

        self.assertEqual(
            [record.internal_sequence for record in started],
            [different.internal_sequence],
        )
        self.assertEqual(
            status.read_status(later_same.internal_sequence).status,
            "QUEUED",
        )
        self.assertEqual(
            status.read_status(different.internal_sequence).status,
            "FAILED",
        )
        self.assertEqual(scheduler.active_count, 0)
        self.assertEqual(scheduler.reserved_event_ids, {"unfinalized"})

    def test_current_journal_blocks_same_event_after_scheduler_recreation(self) -> None:
        first = self._accept("unresolved")

        def leave_malformed_journal(record: status.CalculationRecord) -> None:
            recalculation.prepare_calculation(record.internal_sequence)
            (
                paths.event_service_dir(record.event_id)
                / recalculation.TRANSACTION_FILE
            ).write_text("{broken", encoding="utf-8")

        original = self._scheduler(leave_malformed_journal, capacity=1)
        self.assertEqual(len(original.tick()), 1)
        self._wait_for_errors(original, 1)
        self.assertTrue(original.wait_until_idle(timeout=5))

        journal = (
            paths.event_service_dir("unresolved")
            / recalculation.TRANSACTION_FILE
        )
        evidence = journal.read_bytes()
        current = status.read_current_record("unresolved")
        self.assertEqual(current.internal_sequence, first.internal_sequence)
        self.assertEqual(current.status, "FAILED")

        later_same = self._accept("unresolved")
        different = self._accept("different")
        original.shutdown()

        recreated = self._scheduler(self._complete_failed, capacity=1)
        started = recreated.tick()
        self.assertTrue(recreated.wait_until_idle(timeout=5))

        self.assertEqual(
            [record.internal_sequence for record in started],
            [different.internal_sequence],
        )
        self.assertEqual(
            status.read_status(later_same.internal_sequence).status,
            "QUEUED",
        )
        current = status.read_current_record("unresolved")
        self.assertEqual(current.internal_sequence, first.internal_sequence)
        self.assertEqual(current.status, "FAILED")
        self.assertEqual(journal.read_bytes(), evidence)
        self.assertEqual(
            status.read_status(different.internal_sequence).status,
            "FAILED",
        )
        self.assertEqual(recreated.active_count, 0)

    def test_current_record_without_journal_allows_recalculation(self) -> None:
        first = self._accept("ordinary")

        def prepare_then_return(record: status.CalculationRecord) -> None:
            recalculation.prepare_calculation(record.internal_sequence)

        original = self._scheduler(prepare_then_return, capacity=1)
        self.assertEqual(len(original.tick()), 1)
        self.assertTrue(original.wait_until_idle(timeout=5))
        self.assertEqual(
            status.read_current_record("ordinary").internal_sequence,
            first.internal_sequence,
        )
        self.assertFalse(
            (
                paths.event_service_dir("ordinary")
                / recalculation.TRANSACTION_FILE
            ).exists()
        )
        original.shutdown()

        later = self._accept("ordinary")

        def replace_current(record: status.CalculationRecord) -> None:
            recalculation.prepare_calculation(record.internal_sequence)
            status.transition_current_record(
                record.event_id,
                status.LifecycleState.FAILED,
                failure={"code": "test_completion", "message": "fixture"},
                service_outcome={"completed": True, "successful": False},
            )

        recreated = self._scheduler(replace_current, capacity=1)
        self.assertEqual(
            [record.internal_sequence for record in recreated.tick()],
            [later.internal_sequence],
        )
        self.assertTrue(recreated.wait_until_idle(timeout=5))
        current = status.read_current_record("ordinary")
        self.assertEqual(current.internal_sequence, later.internal_sequence)
        self.assertEqual(current.status, "FAILED")

    def test_transaction_entry_types_block_without_inspection_or_mutation(self) -> None:
        symlink_target = Path(self.temporary.name) / "transaction-target"
        symlink_target.write_bytes(b"target evidence")

        for entry_type in ("regular", "symlink", "directory"):
            with self.subTest(entry_type=entry_type):
                event_id = f"blocked-{entry_type}"
                different_id = f"different-{entry_type}"
                self._promote_failed_current(event_id)
                current = paths.event_service_dir(event_id)
                transaction = current / recalculation.TRANSACTION_FILE
                if entry_type == "regular":
                    transaction.write_bytes(b"{broken")
                elif entry_type == "symlink":
                    transaction.symlink_to(symlink_target)
                else:
                    transaction.mkdir()
                    (transaction / "evidence").write_bytes(b"directory evidence")

                blocked = self._accept(event_id)
                different = self._accept(different_id)
                scheduler = self._scheduler(self._complete_failed, capacity=1)
                started = scheduler.tick()
                self.assertTrue(scheduler.wait_until_idle(timeout=5))

                self.assertEqual(
                    [record.internal_sequence for record in started],
                    [different.internal_sequence],
                )
                self.assertEqual(
                    status.read_status(blocked.internal_sequence).status,
                    "QUEUED",
                )
                self.assertEqual(scheduler.errors, ())
                self.assertEqual(scheduler.reserved_event_ids, frozenset())
                self.assertEqual(scheduler.active_count, 0)
                if entry_type == "regular":
                    self.assertEqual(transaction.read_bytes(), b"{broken")
                elif entry_type == "symlink":
                    self.assertTrue(transaction.is_symlink())
                    self.assertEqual(os.readlink(transaction), str(symlink_target))
                    self.assertEqual(symlink_target.read_bytes(), b"target evidence")
                else:
                    self.assertTrue(transaction.is_dir())
                    self.assertEqual(
                        (transaction / "evidence").read_bytes(),
                        b"directory evidence",
                    )

    def test_current_entry_swap_is_an_event_local_admission_error(self) -> None:
        self._promote_failed_current("entry-swap")
        current = paths.event_service_dir("entry-swap")
        journal = current / recalculation.TRANSACTION_FILE
        journal.write_bytes(b"{broken")
        replacement = paths.events_dir() / ".entry-swap-replacement"
        shutil.copytree(current, replacement)
        (replacement / recalculation.TRANSACTION_FILE).unlink()
        detached = paths.events_dir() / ".entry-swap-detached"
        blocked = self._accept("entry-swap")
        different = self._accept("entry-swap-different")

        original_open_child = recalculation._open_child_directory
        swapped = False

        def swap_before_open(parent, name):
            nonlocal swapped
            if (
                parent.path == paths.events_dir()
                and name == "entry-swap"
                and not swapped
            ):
                swapped = True
                current.rename(detached)
                replacement.rename(current)
            return original_open_child(parent, name)

        scheduler = self._scheduler(self._complete_failed, capacity=1)
        with mock.patch.object(
            recalculation,
            "_open_child_directory",
            side_effect=swap_before_open,
        ):
            started = scheduler.tick()
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

        self.assertEqual(
            [record.internal_sequence for record in started],
            [different.internal_sequence],
        )
        self.assertEqual(
            status.read_status(blocked.internal_sequence).status,
            "QUEUED",
        )
        self.assertEqual(
            (detached / recalculation.TRANSACTION_FILE).read_bytes(),
            b"{broken",
        )
        self.assertEqual(len(scheduler.errors), 1)
        self.assertIn("changed while it was opened", scheduler.errors[0].message)
        self.assertEqual(scheduler.reserved_event_ids, frozenset())
        self.assertEqual(scheduler.active_count, 0)

    def test_events_ancestor_swap_is_an_event_local_admission_error(self) -> None:
        self._promote_failed_current("ancestor-swap")
        events = paths.events_dir()
        replacement = paths.service_dir() / ".events-replacement"
        shutil.copytree(events, replacement)
        replacement_current = replacement / "ancestor-swap"
        replacement_journal = replacement_current / recalculation.TRANSACTION_FILE
        replacement_journal.write_bytes(b"{broken")
        detached = paths.service_dir() / ".events-detached"
        blocked = self._accept("ancestor-swap")
        different = self._accept("ancestor-swap-different")

        original_entry_details = recalculation._entry_details
        swapped = False

        def swap_after_events_open(parent, name):
            nonlocal swapped
            if parent.path == events and name == "ancestor-swap" and not swapped:
                swapped = True
                events.rename(detached)
                replacement.rename(events)
            return original_entry_details(parent, name)

        scheduler = self._scheduler(self._complete_failed, capacity=1)
        with mock.patch.object(
            recalculation,
            "_entry_details",
            side_effect=swap_after_events_open,
        ):
            started = scheduler.tick()
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

        self.assertEqual(
            [record.internal_sequence for record in started],
            [different.internal_sequence],
        )
        self.assertEqual(
            status.read_status(blocked.internal_sequence).status,
            "QUEUED",
        )
        self.assertEqual(
            (
                paths.event_service_dir("ancestor-swap")
                / recalculation.TRANSACTION_FILE
            ).read_bytes(),
            b"{broken",
        )
        self.assertEqual(len(scheduler.errors), 1)
        self.assertIn("changed after it was opened", scheduler.errors[0].message)
        self.assertEqual(scheduler.reserved_event_ids, frozenset())
        self.assertEqual(scheduler.active_count, 0)

    def test_promoted_running_record_is_failed_in_its_current_location(self) -> None:
        accepted = self._accept("promoted")

        def promote_then_return(record: status.CalculationRecord) -> None:
            paths.events_dir().mkdir(parents=True, exist_ok=True)
            paths.queue_entry_dir(record.internal_sequence).rename(
                paths.event_service_dir(record.event_id)
            )

        scheduler = self._scheduler(promote_then_return, capacity=1)

        self.assertEqual(len(scheduler.tick()), 1)
        self.assertTrue(scheduler.wait_until_idle(timeout=5))

        self.assertIsNone(status.read_status(accepted.internal_sequence))
        current = status.read_current_record("promoted")
        self.assertEqual(current.status, "FAILED")
        self.assertEqual(current.internal_sequence, accepted.internal_sequence)
        self.assertEqual(
            current.failure["code"],
            "scheduler_callback_incomplete",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
