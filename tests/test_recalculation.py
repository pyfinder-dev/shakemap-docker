from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from shakemap_service import paths, recalculation, status, worker
from shakemap_service.config import Settings
from shakemap_service.status import LifecycleState
from shakemap_service.submission import Upload, accept_request


ARCHIVE_TIME = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


class RecalculationTests(unittest.TestCase):
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

    def _running_request(
        self,
        event_id: str = "evt",
        *,
        overwrite: bool = True,
        files: dict[str, bytes] | None = None,
    ) -> tuple[int, dict[str, bytes]]:
        payloads = files or {"event.xml": b"new-event", "stationlist.xml": b"new-stations"}
        result = accept_request(
            event_id,
            [Upload(name, io.BytesIO(payload)) for name, payload in payloads.items()],
            overwrite=overwrite,
        )
        status.transition_to_running(result.internal_sequence)
        return result.internal_sequence, payloads

    def _preceding(self, event_id: str, products: bool, service: bool) -> None:
        if products:
            target = paths.event_products_dir(event_id)
            (target / "current" / "products").mkdir(parents=True)
            (target / "current" / "products" / "old.json").write_bytes(b"old-product")
            (target / "stale.txt").write_bytes(b"stale")
        if service:
            target = paths.event_service_dir(event_id)
            (target / "logs").mkdir(parents=True)
            (target / "logs" / "service.log").write_bytes(b"old-service")
            (target / "old-status.json").write_bytes(b"old-record")

    def test_both_overwrite_modes_cover_both_one_and_neither_preceding_tree(self) -> None:
        for overwrite in (True, False):
            for products, service in ((True, True), (True, False), (False, True), (False, False)):
                with self.subTest(overwrite=overwrite, products=products, service=service):
                    self.tearDown()
                    self.setUp()
                    self._preceding("evt", products, service)
                    sequence, payloads = self._running_request(overwrite=overwrite)
                    input_before = {
                        path.name: path.read_bytes()
                        for path in paths.event_input_dir("evt").iterdir()
                    }

                    with mock.patch.object(
                        worker,
                        "execute_shakemap",
                        side_effect=AssertionError("native execution is forbidden"),
                    ):
                        result = recalculation.prepare_calculation(
                            sequence,
                            archive_time=ARCHIVE_TIME,
                        )

                    self.assertEqual(result.record.status, "RUNNING")
                    self.assertEqual(status.read_current_record("evt").internal_sequence, sequence)
                    self.assertFalse(paths.queue_entry_dir(sequence).exists())
                    self.assertEqual(
                        sorted(path.name for path in paths.event_products_dir("evt").iterdir()),
                        ["current"],
                    )
                    current = paths.event_current_dir("evt")
                    self.assertEqual(
                        {path.name: path.read_bytes() for path in current.iterdir()},
                        payloads,
                    )
                    self.assertFalse((paths.event_products_dir("evt") / "stale.txt").exists())
                    self.assertFalse(
                        (paths.event_service_dir("evt") / "old-status.json").exists()
                    )
                    self.assertFalse(
                        (
                            paths.event_service_dir("evt")
                            / recalculation.STAGING_DIRECTORY
                        ).exists()
                    )
                    self.assertEqual(
                        {
                            path.name: path.read_bytes()
                            for path in paths.event_input_dir("evt").iterdir()
                        },
                        input_before,
                    )
                    self.assertTrue(
                        (paths.event_service_dir("evt") / recalculation.TRANSACTION_FILE).is_file()
                    )

                    archives = list(paths.archive_dir().iterdir())
                    if overwrite or not (products or service):
                        self.assertEqual(archives, [])
                    else:
                        self.assertEqual(len(archives), 1)
                        self.assertEqual(
                            {path.name for path in archives[0].iterdir()},
                            {
                                name
                                for name, present in (("products", products), ("service", service))
                                if present
                            },
                        )
                        if products:
                            self.assertEqual(
                                (archives[0] / "products" / "stale.txt").read_bytes(),
                                b"stale",
                            )
                        if service:
                            self.assertEqual(
                                (archives[0] / "service" / "old-status.json").read_bytes(),
                                b"old-record",
                            )

    def test_archive_collision_advances_timestamp_without_overwrite(self) -> None:
        occupied = paths.event_archive_dir("evt", "20260815T120000.000000Z")
        occupied.mkdir(parents=True)
        (occupied / "sentinel").write_bytes(b"keep")
        self._preceding("evt", True, True)
        sequence, _ = self._running_request(overwrite=False)

        result = recalculation.prepare_calculation(sequence, archive_time=ARCHIVE_TIME)

        self.assertEqual((occupied / "sentinel").read_bytes(), b"keep")
        self.assertEqual(
            result.archive_directory,
            paths.event_archive_dir("evt", "20260815T120000.000001Z"),
        )
        self.assertTrue(result.archive_directory.is_dir())

    def test_checksum_mismatch_is_rejected_before_any_tree_mutation(self) -> None:
        self._preceding("evt", True, True)
        sequence, _ = self._running_request(overwrite=False)
        snapshot = paths.queue_request_dir(sequence) / "event.xml"
        snapshot.write_bytes(b"tampered!")
        preceding_products = sorted(
            str(path.relative_to(paths.event_products_dir("evt")))
            for path in paths.event_products_dir("evt").rglob("*")
        )
        preceding_service = sorted(
            str(path.relative_to(paths.event_service_dir("evt")))
            for path in paths.event_service_dir("evt").rglob("*")
        )

        with self.assertRaisesRegex(recalculation.RecalculationError, "snapshot .* differs"):
            recalculation.prepare_calculation(sequence, archive_time=ARCHIVE_TIME)

        self.assertTrue(paths.queue_entry_dir(sequence).is_dir())
        self.assertEqual(
            preceding_products,
            sorted(
                str(path.relative_to(paths.event_products_dir("evt")))
                for path in paths.event_products_dir("evt").rglob("*")
            ),
        )
        self.assertEqual(
            preceding_service,
            sorted(
                str(path.relative_to(paths.event_service_dir("evt")))
                for path in paths.event_service_dir("evt").rglob("*")
            ),
        )
        self.assertFalse(
            (paths.queue_entry_dir(sequence) / recalculation.TRANSACTION_FILE).exists()
        )
        self.assertEqual(list(paths.archive_dir().iterdir()), [])

    def test_rejects_non_running_record_without_journal_or_mutation(self) -> None:
        result = accept_request("evt", [Upload("event.xml", io.BytesIO(b"event"))])

        with self.assertRaisesRegex(recalculation.RecalculationError, "durably RUNNING"):
            recalculation.prepare_calculation(result.internal_sequence)

        self.assertEqual(status.read_status(result.internal_sequence).status, "QUEUED")
        self.assertFalse(
            (
                paths.queue_entry_dir(result.internal_sequence)
                / recalculation.TRANSACTION_FILE
            ).exists()
        )

    def test_finalization_requires_a_terminal_record_and_is_idempotent(self) -> None:
        sequence, _ = self._running_request()
        recalculation.prepare_calculation(sequence)

        with self.assertRaisesRegex(recalculation.RecalculationError, "not terminal"):
            recalculation.finalize_transaction("evt")

        status.transition_current_record(
            "evt",
            LifecycleState.FAILED,
            failure={"code": "native_failed", "message": "injected"},
            service_outcome={"completed": True, "successful": False},
        )
        self.assertTrue(recalculation.finalize_transaction("evt"))
        self.assertFalse(recalculation.finalize_transaction("evt"))
        self.assertEqual(status.read_current_record("evt").status, "FAILED")

    def test_journal_records_missing_counterparts_without_changing_status_schema(self) -> None:
        sequence, _ = self._running_request(overwrite=False)

        recalculation.prepare_calculation(sequence, archive_time=ARCHIVE_TIME)

        journal = json.loads(
            (paths.event_service_dir("evt") / recalculation.TRANSACTION_FILE).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["preceding"], {"products": False, "service": False})
        self.assertIsNone(journal["archive_path"])
        self.assertEqual(journal["completed"], list(recalculation._CHECKPOINTS))
        self.assertIsNone(journal["recovery_failure"])
        record_data = json.loads(paths.event_status_file("evt").read_text(encoding="utf-8"))
        self.assertNotIn("preceding", record_data)
        self.assertNotIn("archive", record_data)

    def test_missing_counterpart_warning_survives_finalization(self) -> None:
        cases = (
            (False, False, "no preceding products or service tree existed"),
            (False, True, "preceding products tree was missing"),
            (True, False, "preceding service tree was missing"),
        )
        for products, service, expected_warning in cases:
            with self.subTest(products=products, service=service):
                self.tearDown()
                self.setUp()
                self._preceding("evt", products, service)
                sequence, _ = self._running_request(overwrite=True)

                recalculation.prepare_calculation(sequence)
                running = status.read_current_record("evt")
                self.assertIn(expected_warning, running.warnings)
                status.transition_current_record(
                    "evt",
                    LifecycleState.FAILED,
                    failure={"code": "native_failed", "message": "injected"},
                    service_outcome={"completed": True, "successful": False},
                )
                recalculation.finalize_transaction("evt")

                self.assertIn(
                    expected_warning,
                    status.read_current_record("evt").warnings,
                )

    def test_finalization_retains_invalid_journals(self) -> None:
        cases = ("malformed", "incomplete", "mismatched", "recovery_failure")
        for case in cases:
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                sequence, _ = self._running_request()
                recalculation.prepare_calculation(sequence)
                status.transition_current_record(
                    "evt",
                    LifecycleState.FAILED,
                    failure={"code": "native_failed", "message": "injected"},
                    service_outcome={"completed": True, "successful": False},
                )
                journal_path = (
                    paths.event_service_dir("evt") / recalculation.TRANSACTION_FILE
                )
                if case == "malformed":
                    journal_path.write_text("{broken", encoding="utf-8")
                else:
                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    if case == "incomplete":
                        journal["completed"].pop()
                    elif case == "mismatched":
                        journal["overwrite"] = not journal["overwrite"]
                    else:
                        journal["recovery_failure"] = {
                            "type": "OSError",
                            "message": "retained evidence",
                            "recorded_at": "2026-08-16T00:00:00.000000Z",
                        }
                    journal_path.write_text(json.dumps(journal), encoding="utf-8")

                with self.assertRaises(recalculation.RecalculationError):
                    recalculation.finalize_transaction("evt")

                self.assertTrue(journal_path.is_file())

    def test_parent_entry_replacement_is_detected_before_mutation(self) -> None:
        self._preceding("evt", True, True)
        sequence, _ = self._running_request(overwrite=False)
        queue_entry = paths.queue_entry_dir(sequence)
        detached = paths.queue_dir() / ".detached-request"
        real_load = recalculation._load_running_record

        def replace_after_status_read(selected_sequence: int):
            record = real_load(selected_sequence)
            queue_entry.rename(detached)
            shutil.copytree(detached, queue_entry)
            return record

        with mock.patch.object(
            recalculation,
            "_load_running_record",
            side_effect=replace_after_status_read,
        ):
            with self.assertRaisesRegex(
                recalculation.RecalculationError,
                "parent entry changed",
            ):
                recalculation.prepare_calculation(sequence, archive_time=ARCHIVE_TIME)

        self.assertTrue(paths.event_products_dir("evt").is_dir())
        self.assertTrue(paths.event_service_dir("evt").is_dir())
        self.assertEqual(list(paths.archive_dir().iterdir()), [])
        self.assertFalse((detached / recalculation.TRANSACTION_FILE).exists())
        self.assertFalse((queue_entry / recalculation.TRANSACTION_FILE).exists())

    def test_parent_entry_replacement_is_detected_at_normal_promotion(self) -> None:
        self._preceding("evt", True, True)
        sequence, _ = self._running_request(overwrite=False)
        queue_entry = paths.queue_entry_dir(sequence)
        detached = paths.queue_dir() / ".detached-at-promotion"
        replaced = False

        def replace_before_promotion(position: str, kind: str, name: str) -> None:
            nonlocal replaced
            if not replaced and (position, kind, name) == (
                "before",
                "rename",
                "record_promoted",
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
                recalculation.prepare_calculation(sequence, archive_time=ARCHIVE_TIME)

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


if __name__ == "__main__":
    unittest.main()
