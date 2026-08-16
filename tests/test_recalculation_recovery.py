from __future__ import annotations

import io
import fcntl
import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from unittest import mock

from shakemap_service import paths, recalculation, status, worker
from shakemap_service.config import Settings
from shakemap_service.directory_access import open_service_directory
from shakemap_service.submission import Upload, accept_request


ARCHIVE_TIME = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


class InjectedInterruption(RuntimeError):
    pass


class RecalculationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_path_settings = paths.settings
        self.previous_status_settings = status.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )
        paths.settings = configured
        status.settings = configured

    def tearDown(self) -> None:
        paths.settings = self.previous_path_settings
        status.settings = self.previous_status_settings
        self.temporary.cleanup()

    def _reset_runtime(self) -> None:
        self.tearDown()
        self.setUp()

    def _running_with_preceding(self, *, overwrite: bool) -> int:
        old_products = paths.event_products_dir("evt")
        (old_products / "current" / "products").mkdir(parents=True)
        (old_products / "current" / "products" / "old.json").write_bytes(b"old")
        old_service = paths.event_service_dir("evt")
        (old_service / "logs").mkdir(parents=True)
        (old_service / "logs" / "service.log").write_bytes(b"old-log")
        result = accept_request(
            "evt",
            [
                Upload("event.xml", io.BytesIO(b"new-event")),
                Upload("stationlist.xml", io.BytesIO(b"new-stations")),
            ],
            overwrite=overwrite,
        )
        status.transition_to_running(result.internal_sequence)
        return result.internal_sequence

    def _leave_initial_journal(self, *, overwrite: bool) -> int:
        sequence = self._running_with_preceding(overwrite=overwrite)
        real_write_journal = recalculation._write_journal
        first_write = True

        def stop_after_journal(record_handle: object, journal: object) -> None:
            nonlocal first_write
            real_write_journal(record_handle, journal)
            if first_write:
                first_write = False
                raise InjectedInterruption("stop after durable journal")

        with mock.patch.object(
            recalculation,
            "_write_journal",
            side_effect=stop_after_journal,
        ):
            with self.assertRaises(InjectedInterruption):
                recalculation.prepare_calculation(sequence, archive_time=ARCHIVE_TIME)
        return sequence

    def _boundaries(self, overwrite: bool) -> list[tuple[str, str, str]]:
        observed: list[tuple[str, str, str]] = []
        sequence = self._running_with_preceding(overwrite=overwrite)
        with mock.patch.object(
            recalculation,
            "_operation_boundary",
            side_effect=lambda position, kind, name: observed.append((position, kind, name)),
        ):
            recalculation.prepare_calculation(sequence, archive_time=ARCHIVE_TIME)
        return observed

    def test_every_named_boundary_reconciles_forward_and_second_run_is_a_noop(self) -> None:
        for overwrite in (True, False):
            boundaries = self._boundaries(overwrite)
            self._reset_runtime()
            for boundary in boundaries:
                with self.subTest(overwrite=overwrite, boundary=boundary):
                    self._reset_runtime()
                    sequence = self._running_with_preceding(overwrite=overwrite)
                    fired = False

                    def interrupt(position: str, kind: str, name: str) -> None:
                        nonlocal fired
                        if not fired and (position, kind, name) == boundary:
                            fired = True
                            raise InjectedInterruption("injected boundary interruption")

                    with mock.patch.object(
                        worker,
                        "execute_shakemap",
                        side_effect=AssertionError("native execution is forbidden"),
                    ), mock.patch.object(
                        recalculation,
                        "_operation_boundary",
                        side_effect=interrupt,
                    ):
                        with self.assertRaises(InjectedInterruption):
                            recalculation.prepare_calculation(
                                sequence,
                                archive_time=ARCHIVE_TIME,
                            )
                    self.assertTrue(fired)

                    with mock.patch.object(
                        worker,
                        "execute_shakemap",
                        side_effect=AssertionError("native execution is forbidden"),
                    ):
                        self.assertTrue(recalculation.reconcile_transaction("evt", sequence))
                        self.assertFalse(recalculation.reconcile_transaction("evt", sequence))

                    current = status.read_current_record("evt")
                    self.assertEqual(current.internal_sequence, sequence)
                    self.assertEqual(current.status, "FAILED")
                    self.assertEqual(current.failure["code"], "interrupted_recalculation")
                    self.assertEqual(
                        current.failure["phase"],
                        current.progress["phase"],
                    )
                    self.assertEqual(
                        current.progress["phase"],
                        "preceding_tree_disposition",
                    )
                    self.assertFalse(paths.queue_entry_dir(sequence).exists())
                    self.assertFalse(
                        (paths.event_service_dir("evt") / recalculation.TRANSACTION_FILE).exists()
                    )
                    self.assertFalse(
                        (
                            paths.event_service_dir("evt")
                            / recalculation.NATIVE_CANDIDATE_DIRECTORY
                        ).exists()
                    )
                    public = paths.event_products_dir("evt")
                    if public.exists():
                        self.assertEqual(
                            sorted(path.name for path in public.iterdir()),
                            ["current"],
                        )
                        self.assertEqual(
                            (public / "current" / "event.xml").read_bytes(),
                            b"new-event",
                        )

    def test_incoherent_state_retains_journal_with_exact_failure_evidence(self) -> None:
        sequence = self._running_with_preceding(overwrite=False)
        real_write_journal = recalculation._write_journal

        def stop_after_journal(record_handle: object, journal: object) -> None:
            real_write_journal(record_handle, journal)
            raise InjectedInterruption("stop after durable journal")

        with mock.patch.object(
            recalculation,
            "_write_journal",
            side_effect=stop_after_journal,
        ):
            with self.assertRaises(InjectedInterruption):
                recalculation.prepare_calculation(sequence, archive_time=ARCHIVE_TIME)

        journal_path = paths.queue_entry_dir(sequence) / recalculation.TRANSACTION_FILE
        if not journal_path.exists():
            self.fail("test injection did not leave a durable journal")
        stage = paths.queue_entry_dir(sequence) / recalculation.STAGING_DIRECTORY
        stage.mkdir()
        (stage / "products").mkdir()

        with self.assertRaisesRegex(recalculation.RecalculationError, "both exist"):
            recalculation.reconcile_transaction("evt", sequence)

        evidence = json.loads(journal_path.read_text(encoding="utf-8"))["recovery_failure"]
        self.assertEqual(evidence["type"], "RecalculationError")
        self.assertIn("both exist", evidence["message"])
        self.assertEqual(status.read_status(sequence).status, "RUNNING")

    def test_tampered_overwrite_or_archive_name_cannot_change_disposition(self) -> None:
        for tamper in ("overwrite", "archive_name"):
            with self.subTest(tamper=tamper):
                self._reset_runtime()
                sequence = self._leave_initial_journal(overwrite=False)
                journal_path = (
                    paths.queue_entry_dir(sequence) / recalculation.TRANSACTION_FILE
                )
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                if tamper == "overwrite":
                    journal["overwrite"] = True
                    journal["archive_path"] = None
                else:
                    journal["archive_path"] = str(
                        paths.event_archive_dir(
                            "other-event",
                            "20260815T120000.000000Z",
                        )
                    )
                journal_path.write_text(json.dumps(journal), encoding="utf-8")

                with self.assertRaises(recalculation.RecalculationError):
                    recalculation.reconcile_transaction("evt", sequence)

                self.assertTrue(paths.event_products_dir("evt").is_dir())
                self.assertTrue(paths.event_service_dir("evt").is_dir())
                self.assertEqual(list(paths.archive_dir().iterdir()), [])
                self.assertTrue(journal_path.is_file())

    def test_partial_recursive_discard_is_completed_from_durable_intent(self) -> None:
        sequence = self._running_with_preceding(overwrite=True)

        def interrupt(position: str, kind: str, name: str) -> None:
            if (position, kind, name) == (
                "after",
                "intent",
                "stage_removal_started",
            ):
                raise InjectedInterruption("stop before recursive discard")

        with mock.patch.object(
            recalculation,
            "_operation_boundary",
            side_effect=interrupt,
        ):
            with self.assertRaises(InjectedInterruption):
                recalculation.prepare_calculation(sequence)

        stage = paths.queue_entry_dir(sequence) / recalculation.STAGING_DIRECTORY
        shutil.rmtree(stage / "products")
        (stage / "service" / "logs" / "service.log").unlink()

        self.assertTrue(recalculation.reconcile_transaction("evt", sequence))

        self.assertFalse(stage.exists())
        self.assertEqual(list(paths.archive_dir().iterdir()), [])
        self.assertFalse(paths.event_products_dir("evt").exists())
        self.assertFalse((paths.event_service_dir("evt") / "logs").exists())
        self.assertEqual(status.read_current_record("evt").status, "FAILED")

    def test_recovery_candidate_removal_can_be_reconciled_after_its_own_crash(self) -> None:
        sequence = self._running_with_preceding(overwrite=True)

        def stop_with_ready_candidate(position: str, kind: str, name: str) -> None:
            if (position, kind, name) == ("after", "checkpoint", "candidate_ready"):
                raise InjectedInterruption("stop with ready candidate")

        with mock.patch.object(
            recalculation,
            "_operation_boundary",
            side_effect=stop_with_ready_candidate,
        ):
            with self.assertRaises(InjectedInterruption):
                recalculation.prepare_calculation(sequence)

        def stop_after_removal(position: str, kind: str, name: str) -> None:
            if (position, kind, name) == (
                "before",
                "fsync",
                "recovery_candidate_removed",
            ):
                raise InjectedInterruption("stop after candidate removal")

        with mock.patch.object(
            recalculation,
            "_operation_boundary",
            side_effect=stop_after_removal,
        ):
            with self.assertRaises(recalculation.RecalculationError):
                recalculation.reconcile_transaction("evt", sequence)

        journal_path = paths.event_service_dir("evt") / recalculation.TRANSACTION_FILE
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertTrue(journal["intents"]["candidate_removal_started"])
        self.assertFalse(
            (
                paths.event_service_dir("evt")
                / recalculation.NATIVE_CANDIDATE_DIRECTORY
            ).exists()
        )

        self.assertTrue(recalculation.reconcile_transaction("evt", sequence))
        self.assertFalse(recalculation.reconcile_transaction("evt", sequence))
        self.assertEqual(status.read_current_record("evt").status, "FAILED")
        self.assertFalse(journal_path.exists())

    def test_recovery_detects_parent_entry_replacement_before_mutation(self) -> None:
        sequence = self._leave_initial_journal(overwrite=False)
        queue_entry = paths.queue_entry_dir(sequence)
        detached = paths.queue_dir() / ".detached-recovery"
        real_read = status.read_status
        replaced = False

        def replace_after_status_read(selected_sequence: int):
            nonlocal replaced
            record = real_read(selected_sequence)
            if not replaced:
                replaced = True
                queue_entry.rename(detached)
                shutil.copytree(detached, queue_entry)
            return record

        with mock.patch.object(
            status,
            "read_status",
            side_effect=replace_after_status_read,
        ):
            with self.assertRaisesRegex(
                recalculation.RecalculationError,
                "parent entry changed",
            ):
                recalculation.reconcile_transaction("evt", sequence)

        self.assertTrue(paths.event_products_dir("evt").is_dir())
        self.assertTrue(paths.event_service_dir("evt").is_dir())
        self.assertEqual(list(paths.archive_dir().iterdir()), [])
        self.assertTrue(
            (queue_entry / recalculation.TRANSACTION_FILE).is_file()
        )

    def test_no_journal_returns_false_before_transaction_parents_exist(self) -> None:
        self.assertFalse(recalculation.reconcile_transaction("evt", 1))

    def test_parent_entry_replacement_is_detected_at_recovery_promotion(self) -> None:
        sequence = self._leave_initial_journal(overwrite=False)
        queue_entry = paths.queue_entry_dir(sequence)
        detached = paths.queue_dir() / ".detached-recovery-promotion"
        replaced = False

        def replace_before_promotion(position: str, kind: str, name: str) -> None:
            nonlocal replaced
            if not replaced and (position, kind, name) == (
                "before",
                "rename",
                "recovery_record_promoted",
            ):
                replaced = True
                queue_entry.rename(detached)
                shutil.copytree(detached, queue_entry)

        with mock.patch.object(
            recalculation,
            "_operation_boundary",
            side_effect=replace_before_promotion,
        ):
            with self.assertRaisesRegex(
                recalculation.RecalculationError,
                "parent entry changed",
            ):
                recalculation.reconcile_transaction("evt", sequence)

        self.assertTrue(replaced)
        self.assertTrue(queue_entry.is_dir())
        self.assertFalse(paths.event_service_dir("evt").exists())
        self.assertFalse(paths.event_products_dir("evt").exists())
        archives = list(paths.archive_dir().iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual(
            {path.name for path in archives[0].iterdir()},
            {"products", "service"},
        )

    def test_stale_running_helper_fails_queue_and_current_records_without_retry(self) -> None:
        result = accept_request(
            "queue-event",
            [Upload("event.xml", io.BytesIO(b"event"))],
        )
        status.transition_to_running(result.internal_sequence)

        self.assertTrue(
            recalculation.recover_stale_running_calculation(
                "queue-event",
                result.internal_sequence,
            )
        )
        queue_record = status.read_status(result.internal_sequence)
        self.assertEqual(queue_record.status, "FAILED")
        self.assertEqual(
            queue_record.failure["code"],
            "interrupted_without_transaction",
        )
        self.assertEqual(queue_record.failure["phase"], queue_record.progress["phase"])
        self.assertEqual(
            queue_record.progress["phase"],
            "preceding_tree_disposition",
        )
        self.assertIsNone(queue_record.progress["current_module"])
        self.assertIn("not retried", queue_record.failure["message"])

        current = accept_request(
            "current-event",
            [Upload("event.xml", io.BytesIO(b"event"))],
        )
        status.transition_to_running(current.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(current.internal_sequence).rename(
            paths.event_service_dir("current-event")
        )

        self.assertTrue(
            recalculation.recover_stale_running_calculation(
                "current-event",
                current.internal_sequence,
            )
        )
        current_record = status.read_current_record("current-event")
        self.assertEqual(current_record.status, "FAILED")
        self.assertEqual(
            current_record.failure["code"],
            "interrupted_without_transaction",
        )
        self.assertEqual(
            current_record.failure["phase"],
            current_record.progress["phase"],
        )
        self.assertEqual(
            current_record.progress["phase"],
            "preceding_tree_disposition",
        )

    def test_stale_running_helper_delegates_matching_journal(self) -> None:
        result = accept_request(
            "evt",
            [Upload("event.xml", io.BytesIO(b"event"))],
        )
        status.transition_to_running(result.internal_sequence)
        real_write_journal = recalculation._write_journal

        def stop_after_journal(record_handle: object, journal: object) -> None:
            real_write_journal(record_handle, journal)
            raise InjectedInterruption("stop after journal")

        with mock.patch.object(
            recalculation,
            "_write_journal",
            side_effect=stop_after_journal,
        ):
            with self.assertRaises(InjectedInterruption):
                recalculation.prepare_calculation(result.internal_sequence)

        with mock.patch.object(
            recalculation,
            "reconcile_transaction",
            return_value=True,
        ) as reconcile:
            self.assertTrue(
                recalculation.recover_stale_running_calculation(
                    "evt",
                    result.internal_sequence,
                )
            )
        reconcile.assert_called_once_with("evt", result.internal_sequence)

    def test_stale_running_helper_leaves_malformed_journal_and_raises(self) -> None:
        result = accept_request(
            "evt",
            [Upload("event.xml", io.BytesIO(b"event"))],
        )
        status.transition_to_running(result.internal_sequence)
        real_write_journal = recalculation._write_journal

        def stop_after_journal(record_handle: object, journal: object) -> None:
            real_write_journal(record_handle, journal)
            raise InjectedInterruption("stop after journal")

        with mock.patch.object(
            recalculation,
            "_write_journal",
            side_effect=stop_after_journal,
        ):
            with self.assertRaises(InjectedInterruption):
                recalculation.prepare_calculation(result.internal_sequence)
        journal_path = (
            paths.queue_entry_dir(result.internal_sequence)
            / recalculation.TRANSACTION_FILE
        )
        journal_path.write_text("{broken", encoding="utf-8")

        with self.assertRaises(recalculation.RecalculationError):
            recalculation.recover_stale_running_calculation(
                "evt",
                result.internal_sequence,
            )

        self.assertTrue(journal_path.is_file())
        self.assertEqual(status.read_status(result.internal_sequence).status, "RUNNING")

    def test_stale_running_helper_leaves_irreconcilable_journal_and_raises(self) -> None:
        result = accept_request(
            "evt",
            [Upload("event.xml", io.BytesIO(b"event"))],
        )
        status.transition_to_running(result.internal_sequence)
        real_write_journal = recalculation._write_journal

        def stop_after_journal(record_handle: object, journal: object) -> None:
            real_write_journal(record_handle, journal)
            raise InjectedInterruption("stop after journal")

        with mock.patch.object(
            recalculation,
            "_write_journal",
            side_effect=stop_after_journal,
        ):
            with self.assertRaises(InjectedInterruption):
                recalculation.prepare_calculation(result.internal_sequence)
        queue_entry = paths.queue_entry_dir(result.internal_sequence)
        journal_path = queue_entry / recalculation.TRANSACTION_FILE
        stage = queue_entry / recalculation.STAGING_DIRECTORY
        (stage / "products").mkdir(parents=True)

        with self.assertRaises(recalculation.RecalculationError):
            recalculation.recover_stale_running_calculation(
                "evt",
                result.internal_sequence,
            )

        self.assertTrue(journal_path.is_file())
        self.assertEqual(status.read_status(result.internal_sequence).status, "RUNNING")

    def test_stale_running_helper_ignores_terminal_and_unmatched_records(self) -> None:
        result = accept_request(
            "evt",
            [Upload("event.xml", io.BytesIO(b"event"))],
        )
        self.assertFalse(
            recalculation.recover_stale_running_calculation(
                "evt",
                result.internal_sequence,
            )
        )
        self.assertFalse(
            recalculation.recover_stale_running_calculation(
                "other",
                result.internal_sequence,
            )
        )
        status.transition_to_running(result.internal_sequence)
        status.transition_to_failed(result.internal_sequence, "terminal")
        self.assertFalse(
            recalculation.recover_stale_running_calculation(
                "evt",
                result.internal_sequence,
            )
        )

    def test_stale_running_helper_does_not_fail_swapped_current_sequence(self) -> None:
        first = accept_request(
            "evt",
            [Upload("event.xml", io.BytesIO(b"first"))],
        )
        status.transition_to_running(first.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(first.internal_sequence).rename(
            paths.event_service_dir("evt")
        )
        second = accept_request(
            "evt",
            [Upload("event.xml", io.BytesIO(b"second"))],
        )
        status.transition_to_running(second.internal_sequence)
        displaced = paths.events_dir() / ".displaced-sequence-one"
        real_recovery = status.fail_matching_running_record_if_child_absent
        swapped = False

        def swap_before_locked_decision(record_directory: object, **kwargs: object):
            nonlocal swapped
            if not swapped and record_directory == paths.event_service_dir("evt"):
                swapped = True
                paths.event_service_dir("evt").rename(displaced)
                paths.queue_entry_dir(second.internal_sequence).rename(
                    paths.event_service_dir("evt")
                )
            return real_recovery(record_directory, **kwargs)

        with mock.patch.object(
            status,
            "fail_matching_running_record_if_child_absent",
            side_effect=swap_before_locked_decision,
        ):
            self.assertFalse(
                recalculation.recover_stale_running_calculation(
                    "evt",
                    first.internal_sequence,
                )
            )

        self.assertTrue(swapped)
        current = status.read_current_record("evt")
        self.assertEqual(current.internal_sequence, second.internal_sequence)
        self.assertEqual(current.status, "RUNNING")

    def test_stale_running_helper_does_not_fail_swapped_queue_event(self) -> None:
        first = accept_request(
            "first-event",
            [Upload("event.xml", io.BytesIO(b"first"))],
        )
        status.transition_to_running(first.internal_sequence)
        second = accept_request(
            "second-event",
            [Upload("event.xml", io.BytesIO(b"second"))],
        )
        status.transition_to_running(second.internal_sequence)
        second_status_path = paths.queue_status_file(second.internal_sequence)
        second_status = json.loads(second_status_path.read_text(encoding="utf-8"))
        second_status["internal_sequence"] = first.internal_sequence
        second_status_path.write_text(json.dumps(second_status), encoding="utf-8")
        displaced = paths.queue_dir() / ".displaced-first-event"
        real_recovery = status.fail_matching_running_record_if_child_absent
        swapped = False

        def swap_before_locked_decision(record_directory: object, **kwargs: object):
            nonlocal swapped
            if not swapped and record_directory == paths.queue_entry_dir(
                first.internal_sequence
            ):
                swapped = True
                paths.queue_entry_dir(first.internal_sequence).rename(displaced)
                paths.queue_entry_dir(second.internal_sequence).rename(
                    paths.queue_entry_dir(first.internal_sequence)
                )
            return real_recovery(record_directory, **kwargs)

        with mock.patch.object(
            status,
            "fail_matching_running_record_if_child_absent",
            side_effect=swap_before_locked_decision,
        ):
            self.assertFalse(
                recalculation.recover_stale_running_calculation(
                    "first-event",
                    first.internal_sequence,
                )
            )

        self.assertTrue(swapped)
        replacement = json.loads(
            paths.queue_status_file(first.internal_sequence).read_text(encoding="utf-8")
        )
        self.assertEqual(replacement["event_id"], "second-event")
        self.assertEqual(replacement["status"], "RUNNING")

    def test_stale_running_failure_excludes_competing_journal_publication(self) -> None:
        result = accept_request(
            "evt",
            [Upload("event.xml", io.BytesIO(b"event"))],
        )
        status.transition_to_running(result.internal_sequence)
        transition_ready = Event()
        allow_transition = Event()
        journal_lock_acquired = Event()
        real_replace = status._replace_record_in_directory

        def pause_terminal_write(*args: object, **kwargs: object) -> None:
            transition_ready.set()
            if not allow_transition.wait(timeout=5):
                raise AssertionError("terminal write was not released")
            real_replace(*args, **kwargs)

        def competing_journal_writer() -> str:
            record_handle = open_service_directory(
                paths.queue_entry_dir(result.internal_sequence),
                create=False,
            )
            try:
                fcntl.flock(record_handle.descriptor, fcntl.LOCK_EX)
                journal_lock_acquired.set()
                record_data = json.loads(
                    paths.queue_status_file(result.internal_sequence).read_text(
                        encoding="utf-8"
                    )
                )
                if record_data["status"] == "RUNNING":
                    (
                        paths.queue_entry_dir(result.internal_sequence)
                        / recalculation.TRANSACTION_FILE
                    ).write_text("{}", encoding="utf-8")
                return record_data["status"]
            finally:
                fcntl.flock(record_handle.descriptor, fcntl.LOCK_UN)
                record_handle.close()

        with mock.patch.object(
            status,
            "_replace_record_in_directory",
            side_effect=pause_terminal_write,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                stale = executor.submit(
                    recalculation.recover_stale_running_calculation,
                    "evt",
                    result.internal_sequence,
                )
                self.assertTrue(transition_ready.wait(timeout=5))
                competing = executor.submit(competing_journal_writer)
                self.assertFalse(journal_lock_acquired.wait(timeout=0.1))
                allow_transition.set()
                self.assertTrue(stale.result(timeout=5))
                self.assertEqual(competing.result(timeout=5), "FAILED")

        self.assertFalse(
            (
                paths.queue_entry_dir(result.internal_sequence)
                / recalculation.TRANSACTION_FILE
            ).exists()
        )
        self.assertEqual(status.read_status(result.internal_sequence).status, "FAILED")


if __name__ == "__main__":
    unittest.main()
