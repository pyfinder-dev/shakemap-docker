from __future__ import annotations

import dataclasses
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from shakemap_service import calculation_log, paths, status
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


class CalculationLogTests(unittest.TestCase):
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

    def _accept(self, event_id: str):
        return accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(event_id.encode("utf-8")))],
        )

    def _promote(self, event_id: str) -> status.CalculationRecord:
        accepted = self._accept(event_id)
        status.transition_to_running(accepted.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(accepted.internal_sequence).rename(
            paths.event_service_dir(event_id)
        )
        current = status.read_current_record(event_id)
        if current is None:
            raise AssertionError("promoted test record is missing")
        return current

    def _read_lines(self, event_id: str) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in paths.event_service_log_file(event_id)
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    def assert_utc_timestamp(self, value: object) -> None:
        self.assertIsInstance(value, str)
        timestamp = str(value)
        self.assertTrue(timestamp.endswith("Z"))
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
        self.assertIsNotNone(parsed.tzinfo)

    def test_appends_exact_private_json_lines_and_returns_immutable_evidence(
        self,
    ) -> None:
        record = self._promote("evt")
        service_root = paths.event_service_dir("evt")
        before = set(service_root.rglob("*"))
        status_before = paths.event_status_file("evt").read_bytes()
        environment_before = dict(os.environ)

        first = calculation_log.append_service_log(
            record,
            phase="calculation_preparation",
            severity="INFO",
            message="profile materialization started",
        )
        first_bytes = paths.event_service_log_file("evt").read_bytes()
        second = calculation_log.append_service_log(
            record,
            phase="native_execution",
            severity="WARNING",
            message="native warning: café\ncontinued",
        )

        log_directory = paths.event_logs_dir("evt")
        log_file = paths.event_service_log_file("evt")
        self.assertEqual(
            set(service_root.rglob("*")) - before,
            {log_directory, log_file},
        )
        self.assertEqual(stat.S_IMODE(log_directory.stat().st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE(log_file.stat().st_mode) & 0o077, 0)
        self.assertTrue(log_file.read_bytes().startswith(first_bytes))
        self.assertEqual(first.log_file, log_file)
        self.assertEqual(second.log_file, log_file)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.message = "changed"  # type: ignore[misc]

        lines = self._read_lines("evt")
        self.assertEqual(len(lines), 2)
        expected_common = {
            "event_id": "evt",
            "internal_sequence": record.internal_sequence,
        }
        self.assertEqual(
            lines[0],
            {
                **expected_common,
                "recorded_at": first.recorded_at,
                "phase": "calculation_preparation",
                "severity": "INFO",
                "message": "profile materialization started",
            },
        )
        self.assertEqual(
            lines[1],
            {
                **expected_common,
                "recorded_at": second.recorded_at,
                "phase": "native_execution",
                "severity": "WARNING",
                "message": "native warning: café\ncontinued",
            },
        )
        self.assert_utc_timestamp(lines[0]["recorded_at"])
        self.assert_utc_timestamp(lines[1]["recorded_at"])
        self.assertEqual(paths.event_status_file("evt").read_bytes(), status_before)
        self.assertEqual(dict(os.environ), environment_before)
        self.assertFalse(paths.event_products_dir("evt").exists())

    def test_rejects_invalid_events_and_nonrunning_records_before_writing(
        self,
    ) -> None:
        record = self._promote("invalid-event")
        for field, values in {
            "phase": ("", None),
            "severity": ("", 1),
            "message": ("", object()),
        }.items():
            for value in values:
                arguments = {
                    "phase": "native_execution",
                    "severity": "INFO",
                    "message": "started",
                }
                arguments[field] = value
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, "nonempty string"):
                        calculation_log.append_service_log(record, **arguments)  # type: ignore[arg-type]
        self.assertFalse(paths.event_logs_dir("invalid-event").exists())

        queued = self._accept("queued")
        queued_record = status.read_status(queued.internal_sequence)
        if queued_record is None:
            raise AssertionError("queued test record is missing")
        with self.assertRaisesRegex(ValueError, "must be RUNNING"):
            calculation_log.append_service_log(
                queued_record,
                phase="calculation_preparation",
                severity="INFO",
                message="started",
            )
        self.assertFalse(paths.event_logs_dir("queued").exists())

    def test_requires_matching_running_current_record_before_writing(self) -> None:
        missing = self._accept("missing")
        missing_record = status.transition_to_running(missing.internal_sequence)
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            calculation_log.append_service_log(
                missing_record,
                phase="calculation_preparation",
                severity="INFO",
                message="started",
            )
        self.assertFalse(paths.event_logs_dir("missing").exists())

        self._promote("identity")
        later = self._accept("identity")
        later_record = status.transition_to_running(later.internal_sequence)
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            calculation_log.append_service_log(
                later_record,
                phase="calculation_preparation",
                severity="INFO",
                message="started",
            )
        self.assertFalse(paths.event_logs_dir("identity").exists())

        running = self._promote("terminal")
        status.transition_current_record(
            "terminal",
            status.LifecycleState.FAILED,
            failure={"code": "test_failure", "message": "fixture"},
            service_outcome={"completed": True, "successful": False},
        )
        with self.assertRaisesRegex(ValueError, "current calculation record must"):
            calculation_log.append_service_log(
                running,
                phase="record_finalization",
                severity="ERROR",
                message="failed",
            )
        self.assertFalse(paths.event_logs_dir("terminal").exists())

    def test_different_calculations_are_disjoint(self) -> None:
        first = self._promote("first")
        second = self._promote("second")

        first_event = calculation_log.append_service_log(
            first,
            phase="calculation_preparation",
            severity="INFO",
            message="first",
        )
        second_event = calculation_log.append_service_log(
            second,
            phase="calculation_preparation",
            severity="INFO",
            message="second",
        )

        self.assertNotEqual(first_event.log_file, second_event.log_file)
        self.assertFalse(first_event.log_file.is_relative_to(paths.event_service_dir("second")))
        self.assertFalse(second_event.log_file.is_relative_to(paths.event_service_dir("first")))
        self.assertEqual(self._read_lines("first")[0]["message"], "first")
        self.assertEqual(self._read_lines("second")[0]["message"], "second")

    def test_filesystem_write_and_fsync_failures_propagate_without_other_mutation(
        self,
    ) -> None:
        directory_failure = self._promote("directory-failure")
        directory_status = paths.event_status_file("directory-failure").read_bytes()
        paths.event_logs_dir("directory-failure").write_bytes(b"blocked")
        with self.assertRaises((ValueError, NotADirectoryError)):
            calculation_log.append_service_log(
                directory_failure,
                phase="calculation_preparation",
                severity="ERROR",
                message="failure",
            )
        self.assertEqual(
            paths.event_status_file("directory-failure").read_bytes(),
            directory_status,
        )

        write_failure = self._promote("write-failure")
        write_status = paths.event_status_file("write-failure").read_bytes()
        write_product = paths.event_products_dir("write-failure") / "marker"
        write_product.parent.mkdir(parents=True)
        write_product.write_bytes(b"native")
        with mock.patch.object(
            calculation_log.os,
            "write",
            side_effect=OSError("injected write failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                calculation_log.append_service_log(
                    write_failure,
                    phase="native_execution",
                    severity="ERROR",
                    message="failure",
                )
        self.assertEqual(paths.event_status_file("write-failure").read_bytes(), write_status)
        self.assertEqual(write_product.read_bytes(), b"native")

        fsync_failure = self._promote("fsync-failure")
        fsync_status = paths.event_status_file("fsync-failure").read_bytes()
        paths.event_logs_dir("fsync-failure").mkdir()
        with mock.patch.object(
            calculation_log.os,
            "fsync",
            side_effect=OSError("injected fsync failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected fsync failure"):
                calculation_log.append_service_log(
                    fsync_failure,
                    phase="native_execution",
                    severity="ERROR",
                    message="failure",
                )
        self.assertEqual(paths.event_status_file("fsync-failure").read_bytes(), fsync_status)


if __name__ == "__main__":
    unittest.main()
