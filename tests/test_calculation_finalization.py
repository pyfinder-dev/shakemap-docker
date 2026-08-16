from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from shakemap_service import calculation_finalization, paths, status
from shakemap_service.config import Settings
from shakemap_service.product_validation import (
    ProductValidationResult,
    RequiredProductCheck,
)
from shakemap_service.runner import ExecutionResult
from shakemap_service.submission import Upload, accept_request


TERMINAL_TIMESTAMP = "2026-08-16T15:00:07.000000Z"


class CalculationFinalizationTests(unittest.TestCase):
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

    def _promote(self, event_id: str) -> status.CalculationRecord:
        accepted = accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(event_id.encode("utf-8")))],
        )
        status.transition_to_running(accepted.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(accepted.internal_sequence).rename(
            paths.event_service_dir(event_id)
        )
        current = status.read_current_record(event_id)
        if current is None:
            raise AssertionError("promoted current record is missing")
        return current

    @staticmethod
    def _execution(
        *,
        exit_code: int | None = 0,
        signal: int | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            command=[
                "shake",
                "event",
                "select",
                "assemble",
                "model",
                "contour",
                "mapping",
                "stations",
                "gridxml",
            ],
            exit_code=exit_code,
            signal=signal,
            pid=1234,
            started_at="2026-08-16T15:00:01.000000Z",
            completed_at="2026-08-16T15:00:05.000000Z",
        )

    @staticmethod
    def _validation(*, passed: bool = True) -> ProductValidationResult:
        check = RequiredProductCheck(
            path="core.bin",
            size=4 if passed else None,
            passed=passed,
            reason="generic checks passed" if passed else "product is missing",
        )
        return ProductValidationResult(
            required_paths=("core.bin",),
            source="configured",
            checks=(check,),
            passed=passed,
        )

    def _write_success_evidence(
        self,
        record: status.CalculationRecord,
    ) -> None:
        paths.event_manifest_file(record.event_id).write_text(
            json.dumps(
                {
                    "event_id": record.event_id,
                    "internal_sequence": record.internal_sequence,
                    "partial": False,
                    "uninspected_manifest_content": {"invalid": object.__name__},
                }
            ),
            encoding="utf-8",
        )
        paths.event_provenance_file(record.event_id).write_text(
            json.dumps(
                {
                    "event_id": record.event_id,
                    "internal_sequence": record.internal_sequence,
                    "timestamps": {"terminal_at": TERMINAL_TIMESTAMP},
                    "uninspected_provenance_content": [None, False],
                }
            ),
            encoding="utf-8",
        )
        paths.event_logs_dir(record.event_id).mkdir()
        paths.event_service_log_file(record.event_id).write_bytes(b"service\n")
        paths.event_log_file(record.event_id).write_bytes(b"native\n")

    def _finalize_success(
        self,
        record: status.CalculationRecord,
    ) -> status.CalculationRecord:
        return calculation_finalization.finalize_success(
            record,
            execution=self._execution(),
            validation=self._validation(),
            terminal_timestamp=TERMINAL_TIMESTAMP,
        )

    def test_success_is_the_final_write_with_exact_outcomes_and_paths(
        self,
    ) -> None:
        record = self._promote("success")
        self._write_success_evidence(record)
        actions: list[str] = []
        real_read = calculation_finalization._read_service_json
        real_log = calculation_finalization._require_nonempty_log
        real_terminal = status._transition_current_record_terminal

        def tracked_read(path: Path, label: str):
            actions.append(label)
            return real_read(path, label)

        def tracked_log(path: Path, label: str) -> None:
            actions.append(label)
            real_log(path, label)

        def tracked_terminal(*args, **kwargs):
            actions.append("terminal write")
            return real_terminal(*args, **kwargs)

        with (
            mock.patch.object(
                calculation_finalization,
                "_read_service_json",
                side_effect=tracked_read,
            ),
            mock.patch.object(
                calculation_finalization,
                "_require_nonempty_log",
                side_effect=tracked_log,
            ),
            mock.patch.object(
                status,
                "_transition_current_record_terminal",
                side_effect=tracked_terminal,
            ),
            mock.patch.object(
                status,
                "_now_iso",
                side_effect=AssertionError("terminal timestamp was reselected"),
            ),
        ):
            finalized = self._finalize_success(record)

        self.assertEqual(
            actions,
            [
                "product manifest",
                "provenance",
                "service.log",
                "shake.log",
                "terminal write",
            ],
        )
        self.assertEqual(finalized.status, "SUCCESS")
        self.assertEqual(finalized.timestamps["completed_at"], TERMINAL_TIMESTAMP)
        self.assertEqual(
            finalized.native_outcome,
            {"started": True, "exit_code": 0, "signal": None},
        )
        self.assertEqual(
            finalized.service_outcome,
            {"completed": True, "successful": True},
        )
        self.assertIsNone(finalized.failure)
        self.assertEqual(
            finalized.shared_paths,
            {
                "input": "/operator/runtime/shakemap/data/inputs/success",
                "products": (
                    "/operator/runtime/shakemap/products/success/"
                    "current/products"
                ),
                "provenance": (
                    "/operator/runtime/shakemap/.service/events/success/"
                    "provenance.json"
                ),
                "product_manifest": (
                    "/operator/runtime/shakemap/.service/events/success/"
                    "product-manifest.json"
                ),
                "service_log": (
                    "/operator/runtime/shakemap/.service/events/success/"
                    "logs/service.log"
                ),
                "shake_log": (
                    "/operator/runtime/shakemap/.service/events/success/"
                    "logs/shake.log"
                ),
            },
        )
        self.assertEqual(
            status.read_current_record("success").timestamps["completed_at"],
            TERMINAL_TIMESTAMP,
        )

    def test_native_and_validation_failures_leave_running_record_unchanged(
        self,
    ) -> None:
        cases = (
            ("native-exit", self._execution(exit_code=7), self._validation()),
            (
                "native-signal",
                self._execution(exit_code=None, signal=15),
                self._validation(),
            ),
            ("validation", self._execution(), self._validation(passed=False)),
        )
        for event_id, execution, validation in cases:
            with self.subTest(event_id=event_id):
                record = self._promote(event_id)
                before = paths.event_status_file(event_id).read_bytes()
                with self.assertRaises(
                    calculation_finalization.CalculationFinalizationError
                ):
                    calculation_finalization.finalize_success(
                        record,
                        execution=execution,
                        validation=validation,
                        terminal_timestamp=TERMINAL_TIMESTAMP,
                    )
                self.assertEqual(paths.event_status_file(event_id).read_bytes(), before)
                self.assertEqual(status.read_current_record(event_id).status, "RUNNING")

    def test_manifest_must_be_complete_and_match_current_identity(self) -> None:
        for event_id, change, message in (
            ("manifest-missing", "missing", "unavailable"),
            ("manifest-partial", "partial", "not complete"),
            ("manifest-stale", "identity", "identity does not match"),
        ):
            with self.subTest(event_id=event_id):
                record = self._promote(event_id)
                self._write_success_evidence(record)
                if change == "missing":
                    paths.event_manifest_file(event_id).unlink()
                else:
                    payload = json.loads(
                        paths.event_manifest_file(event_id).read_text()
                    )
                    if change == "partial":
                        payload["partial"] = True
                    else:
                        payload["internal_sequence"] += 1
                    paths.event_manifest_file(event_id).write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                with self.assertRaisesRegex(
                    calculation_finalization.CalculationFinalizationError,
                    message,
                ):
                    self._finalize_success(record)
                self.assertEqual(status.read_current_record(event_id).status, "RUNNING")

    def test_provenance_must_match_identity_and_terminal_timestamp(self) -> None:
        for event_id, change, message in (
            ("provenance-missing", "missing", "unavailable"),
            ("provenance-stale", "identity", "identity does not match"),
            ("provenance-time", "timestamp", "timestamp does not match"),
        ):
            with self.subTest(event_id=event_id):
                record = self._promote(event_id)
                self._write_success_evidence(record)
                if change == "missing":
                    paths.event_provenance_file(event_id).unlink()
                else:
                    payload = json.loads(
                        paths.event_provenance_file(event_id).read_text()
                    )
                    if change == "identity":
                        payload["event_id"] = "other"
                    else:
                        payload["timestamps"]["terminal_at"] = (
                            "2026-08-16T15:00:08.000000Z"
                        )
                    paths.event_provenance_file(event_id).write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                with self.assertRaisesRegex(
                    calculation_finalization.CalculationFinalizationError,
                    message,
                ):
                    self._finalize_success(record)
                self.assertEqual(status.read_current_record(event_id).status, "RUNNING")

    def test_both_logs_must_be_regular_readable_and_nonempty(self) -> None:
        cases = (
            ("service-empty", "service", "empty"),
            ("shake-missing", "shake", "unavailable"),
            ("service-directory", "service-directory", "regular file"),
            ("shake-unreadable", "shake-unreadable", "unreadable"),
        )
        real_open = Path.open
        for event_id, change, message in cases:
            with self.subTest(event_id=event_id):
                record = self._promote(event_id)
                self._write_success_evidence(record)
                unreadable: Path | None = None
                if change == "service":
                    paths.event_service_log_file(event_id).write_bytes(b"")
                elif change == "shake":
                    paths.event_log_file(event_id).unlink()
                elif change == "service-directory":
                    paths.event_service_log_file(event_id).unlink()
                    paths.event_service_log_file(event_id).mkdir()
                else:
                    unreadable = paths.event_log_file(event_id)

                def controlled_open(path: Path, *args, **kwargs):
                    if path == unreadable:
                        raise PermissionError("injected unreadable log")
                    return real_open(path, *args, **kwargs)

                with (
                    mock.patch.object(Path, "open", new=controlled_open),
                    self.assertRaisesRegex(
                        calculation_finalization.CalculationFinalizationError,
                        message,
                    ),
                ):
                    self._finalize_success(record)
                self.assertEqual(status.read_current_record(event_id).status, "RUNNING")

    def test_failure_preserves_primary_secondary_and_native_outcomes(
        self,
    ) -> None:
        record = self._promote("failed")
        secondary = (
            {"phase": "product_manifest", "message": "manifest unavailable"},
            {"phase": "logging", "message": "shake.log unavailable"},
        )
        secondary_before = copy.deepcopy(secondary)

        finalized = calculation_finalization.finalize_failure(
            record,
            terminal_timestamp=TERMINAL_TIMESTAMP,
            phase="native_execution",
            code="native_exit",
            message="ShakeMap exited with code 2",
            execution=self._execution(exit_code=2),
            secondary_evidence=secondary,
        )

        self.assertEqual(finalized.status, "FAILED")
        self.assertEqual(finalized.timestamps["completed_at"], TERMINAL_TIMESTAMP)
        self.assertEqual(
            finalized.failure,
            {
                "phase": "native_execution",
                "code": "native_exit",
                "message": "ShakeMap exited with code 2",
                "secondary_evidence": list(secondary),
            },
        )
        self.assertEqual(
            finalized.native_outcome,
            {"started": True, "exit_code": 2, "signal": None},
        )
        self.assertEqual(
            finalized.service_outcome,
            {"completed": True, "successful": False},
        )
        self.assertEqual(secondary, secondary_before)
        for name in (
            "provenance",
            "product_manifest",
            "service_log",
            "shake_log",
        ):
            self.assertIsNone(finalized.shared_paths[name])

    def test_failure_before_native_start_needs_no_optional_evidence(self) -> None:
        record = self._promote("pre-native")

        finalized = calculation_finalization.finalize_failure(
            record,
            terminal_timestamp=TERMINAL_TIMESTAMP,
            phase="calculation_preparation",
            code="profile_materialization_failed",
            message="selected profile could not be materialized",
        )

        self.assertEqual(finalized.status, "FAILED")
        self.assertIsNone(finalized.native_outcome)
        self.assertEqual(
            finalized.failure["message"],
            "selected profile could not be materialized",
        )

    def test_stale_and_terminal_current_records_are_rejected(self) -> None:
        stale = self._promote("stale")
        stale_supplied = replace(
            stale,
            internal_sequence=stale.internal_sequence + 1,
        )
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            calculation_finalization.finalize_failure(
                stale_supplied,
                terminal_timestamp=TERMINAL_TIMESTAMP,
                phase="record_finalization",
                code="stale",
                message="stale record",
            )
        self.assertEqual(status.read_current_record("stale").status, "RUNNING")

        terminal = self._promote("terminal")
        status.transition_current_record(
            "terminal",
            status.LifecycleState.FAILED,
            failure={"code": "existing", "message": "already failed"},
            service_outcome={"completed": True, "successful": False},
        )
        with self.assertRaisesRegex(ValueError, "must be RUNNING"):
            calculation_finalization.finalize_failure(
                terminal,
                terminal_timestamp=TERMINAL_TIMESTAMP,
                phase="record_finalization",
                code="late",
                message="late finalization",
            )

    def test_private_terminal_writer_requires_exact_current_sequence(self) -> None:
        record = self._promote("private-stale")
        before = paths.event_status_file("private-stale").read_bytes()

        with self.assertRaisesRegex(ValueError, "does not match"):
            status._transition_current_record_terminal(
                record.event_id,
                record.internal_sequence + 1,
                status.LifecycleState.FAILED,
                terminal_timestamp=TERMINAL_TIMESTAMP,
                native_outcome=None,
                service_outcome={"completed": True, "successful": False},
                failure={"code": "failure", "message": "failure"},
                shared_paths=record.shared_paths,
            )

        self.assertEqual(paths.event_status_file("private-stale").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
