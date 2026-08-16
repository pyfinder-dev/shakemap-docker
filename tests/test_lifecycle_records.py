from __future__ import annotations

import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event, Lock
from typing import Any
from unittest import mock

from shakemap_service import paths, status
from shakemap_service.config import Settings
from shakemap_service.status import (
    LifecycleState,
    scan_queue_records,
    transition_status,
)
from shakemap_service.submission import Upload, accept_request


class LifecycleRecordTests(unittest.TestCase):
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

    def _accept(self, event_id: str = "evt"):
        return accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(b"opaque"))],
        )

    def _accept_directory(self, event_id: str, *basenames: str):
        directory = paths.event_input_dir(event_id)
        directory.mkdir(parents=True)
        for basename in ("event.xml", *basenames):
            (directory / basename).write_bytes(basename.encode("utf-8"))
        return accept_request(event_id)

    def _manifest(self, sequence: int) -> tuple[Path, dict[str, Any]]:
        target = paths.queue_entry_dir(sequence) / "request-manifest.json"
        return target, json.loads(target.read_text(encoding="utf-8"))

    def _store_manifest(self, sequence: int, manifest: dict[str, Any]) -> None:
        target = paths.queue_entry_dir(sequence) / "request-manifest.json"
        target.write_text(json.dumps(manifest), encoding="utf-8")

    def _scan_errors(self) -> tuple[list[status.CalculationRecord], dict[str, str]]:
        records, errors = scan_queue_records()
        return records, dict(errors)

    def test_queued_record_round_trip_has_only_required_artifacts(self) -> None:
        result = self._accept("evt:α")

        record = status.read_status(result.internal_sequence)

        self.assertEqual(record.event_id, "evt:α")
        self.assertEqual(record.internal_sequence, 1)
        self.assertEqual(record.status, "QUEUED")
        self.assertTrue(record.overwrite)
        self.assertEqual(record.warnings, [])
        self.assertEqual(record.request["input_mode"], "upload")
        self.assertEqual(record.configuration, {"selected": "global"})
        self.assertIsNone(record.progress["phase"])
        self.assertEqual(
            record.progress["module_plan"],
            ["select", "assemble", "model", "contour", "mapping", "stations", "gridxml"],
        )
        self.assertTrue(record.timestamps["submitted_at"].endswith("Z"))
        self.assertEqual(
            record.shared_paths["input"],
            "/operator/runtime/shakemap/data/inputs/evt:α",
        )
        entry_names = {
            path.name for path in paths.queue_entry_dir(result.internal_sequence).iterdir()
        }
        self.assertEqual(
            entry_names,
            {"request", "request-manifest.json", "status.json"},
        )

    def test_malformed_records_are_reported_without_hiding_valid_work(self) -> None:
        missing = self._accept("missing")
        malformed_status = self._accept("malformed-status")
        malformed_manifest = self._accept("malformed-manifest")
        valid = self._accept("valid")
        paths.queue_status_file(missing.internal_sequence).unlink()
        paths.queue_status_file(malformed_status.internal_sequence).write_text(
            "{broken",
            encoding="utf-8",
        )
        manifest_path, _ = self._manifest(malformed_manifest.internal_sequence)
        manifest_path.write_text("{broken", encoding="utf-8")
        paths.queue_dir().joinpath("visible-invalid-entry").mkdir()
        paths.queue_dir().joinpath(".private-state").mkdir()

        records, errors = scan_queue_records()

        self.assertEqual(
            [(record.internal_sequence, record.event_id) for record in records],
            [(valid.internal_sequence, "valid")],
        )
        self.assertEqual(
            [entry for entry, _ in errors],
            [
                paths.queue_entry_name(missing.internal_sequence),
                paths.queue_entry_name(malformed_status.internal_sequence),
                paths.queue_entry_name(malformed_manifest.internal_sequence),
                "visible-invalid-entry",
            ],
        )
        self.assertIn("status.json is missing or unsafe", errors[0][1])
        self.assertIn("malformed calculation record", errors[1][1])
        self.assertIn("request manifest is malformed", errors[2][1])
        self.assertIn("20 ASCII digits", errors[3][1])

    def test_running_and_failed_transitions_are_durable_and_terminal(self) -> None:
        result = self._accept()

        running = transition_status(result.internal_sequence, LifecycleState.RUNNING)
        failed = transition_status(
            result.internal_sequence,
            LifecycleState.FAILED,
            failure={"code": "test_failure", "message": "fixture"},
            service_outcome={"completed": True, "successful": False},
        )

        self.assertEqual(running.status, "RUNNING")
        self.assertIsNotNone(running.timestamps["started_at"])
        self.assertEqual(failed.status, "FAILED")
        self.assertIsNotNone(failed.timestamps["completed_at"])
        self.assertEqual(status.read_status(result.internal_sequence).status, "FAILED")
        for target in LifecycleState:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    transition_status(result.internal_sequence, target)

    def test_public_success_transitions_reject_before_record_mutation(self) -> None:
        queued = self._accept("queued-success-closed")
        transition_status(queued.internal_sequence, LifecycleState.RUNNING)
        queued_file = paths.queue_status_file(queued.internal_sequence)
        queued_before = queued_file.read_bytes()

        with mock.patch.object(
            status,
            "_transition_record_directory",
            side_effect=AssertionError("public success reached record writer"),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "SUCCESS requires calculation finalization",
            ):
                transition_status(queued.internal_sequence, LifecycleState.SUCCESS)
        self.assertEqual(queued_file.read_bytes(), queued_before)

        current = self._accept("current-success-closed")
        transition_status(current.internal_sequence, LifecycleState.RUNNING)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(current.internal_sequence).rename(
            paths.event_service_dir("current-success-closed")
        )
        current_file = paths.event_status_file("current-success-closed")
        current_before = current_file.read_bytes()

        with mock.patch.object(
            status,
            "_transition_record_directory",
            side_effect=AssertionError("public success reached record writer"),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "SUCCESS requires calculation finalization",
            ):
                status.transition_current_record(
                    "current-success-closed",
                    LifecycleState.SUCCESS,
                )
        self.assertEqual(current_file.read_bytes(), current_before)

    def test_forbidden_transitions_and_invalid_failure_leave_status_unchanged(self) -> None:
        result = self._accept()
        for target in (
            LifecycleState.QUEUED,
            LifecycleState.SUCCESS,
            LifecycleState.FAILED,
        ):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    transition_status(result.internal_sequence, target)
        self.assertEqual(status.read_status(result.internal_sequence).status, "QUEUED")

        transition_status(result.internal_sequence, LifecycleState.RUNNING)
        with self.assertRaisesRegex(ValueError, "FAILED records require failure"):
            transition_status(result.internal_sequence, LifecycleState.FAILED)
        self.assertEqual(status.read_status(result.internal_sequence).status, "RUNNING")

    def test_free_form_update_cannot_change_identity_or_lifecycle(self) -> None:
        result = self._accept()
        for field, value in (
            ("event_id", "other"),
            ("internal_sequence", 99),
            ("status", "SUCCESS"),
            ("schema_version", 2),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "dedicated operations"):
                    status.update_status(result.internal_sequence, **{field: value})

    def test_atomic_write_failure_preserves_preceding_json(self) -> None:
        result = self._accept()
        target = paths.queue_status_file(result.internal_sequence)
        preceding = target.read_bytes()
        real_replace = status.os.replace

        def fail_record_replace(
            source: object,
            destination: object,
            **kwargs: object,
        ) -> None:
            if Path(destination).name == "status.json":
                raise OSError("injected record publication failure")
            real_replace(source, destination, **kwargs)

        with mock.patch.object(status.os, "replace", side_effect=fail_record_replace):
            with self.assertRaisesRegex(OSError, "injected record publication failure"):
                transition_status(result.internal_sequence, LifecycleState.RUNNING)

        self.assertEqual(target.read_bytes(), preceding)
        self.assertEqual(status.read_status(result.internal_sequence).status, "QUEUED")
        self.assertFalse(
            any(path.name.startswith(".status-") for path in target.parent.iterdir())
        )

    def test_concurrent_transitions_are_serialized_by_the_record_directory(self) -> None:
        result = self._accept()
        first_writer_ready = Event()
        allow_first_writer = Event()
        selection_lock = Lock()
        first_writer_selected = False
        real_replace = status._replace_record_in_directory

        def pause_first_writer(*args: object, **kwargs: object) -> None:
            nonlocal first_writer_selected
            with selection_lock:
                pause = not first_writer_selected
                first_writer_selected = True
            if pause:
                first_writer_ready.set()
                if not allow_first_writer.wait(timeout=5):
                    raise AssertionError("first lifecycle writer was not released")
            real_replace(*args, **kwargs)

        with mock.patch.object(
            status,
            "_replace_record_in_directory",
            side_effect=pause_first_writer,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    transition_status,
                    result.internal_sequence,
                    LifecycleState.RUNNING,
                )
                self.assertTrue(first_writer_ready.wait(timeout=5))
                second = executor.submit(
                    transition_status,
                    result.internal_sequence,
                    LifecycleState.RUNNING,
                )
                with self.assertRaises(TimeoutError):
                    second.result(timeout=0.1)
                allow_first_writer.set()
                self.assertEqual(first.result(timeout=5).status, "RUNNING")
                with self.assertRaisesRegex(ValueError, "RUNNING -> RUNNING"):
                    second.result(timeout=5)

        self.assertEqual(status.read_status(result.internal_sequence).status, "RUNNING")

    def test_current_record_read_uses_the_same_schema_and_store_behavior(self) -> None:
        result = self._accept()
        current_directory = paths.event_service_dir("evt")
        current_directory.parent.mkdir(parents=True)
        paths.queue_entry_dir(result.internal_sequence).rename(current_directory)

        current = status.read_current_record("evt")
        updated = status.update_current_record("evt", warnings=["retained"])
        running = status.transition_current_record("evt", LifecycleState.RUNNING)

        self.assertEqual(current.event_id, "evt")
        self.assertEqual(current.internal_sequence, result.internal_sequence)
        self.assertEqual(updated.warnings, ["retained"])
        self.assertEqual(running.status, "RUNNING")
        self.assertEqual(status.read_current_record("evt").status, "RUNNING")

        redirected = paths.event_service_dir("redirected")
        redirected.symlink_to(current_directory, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "record directory is missing or unsafe"):
            status.read_current_record("redirected")

    def test_current_record_scan_reports_unsafe_and_mismatched_entries(self) -> None:
        paths.events_dir().mkdir(parents=True)

        valid = self._accept("valid")
        paths.queue_entry_dir(valid.internal_sequence).rename(
            paths.event_service_dir("valid")
        )

        dot_prefixed = self._accept(".valid")
        paths.queue_entry_dir(dot_prefixed.internal_sequence).rename(
            paths.event_service_dir(".valid")
        )

        mismatched = self._accept("original")
        paths.queue_entry_dir(mismatched.internal_sequence).rename(
            paths.event_service_dir("mismatched")
        )

        malformed = self._accept("malformed")
        paths.queue_entry_dir(malformed.internal_sequence).rename(
            paths.event_service_dir("malformed")
        )
        paths.event_status_file("malformed").write_text("{broken", encoding="utf-8")

        paths.events_dir().joinpath("not-a-directory").write_text(
            "unsafe",
            encoding="utf-8",
        )
        paths.events_dir().joinpath("redirected").symlink_to(
            paths.event_service_dir("valid"),
            target_is_directory=True,
        )
        paths.events_dir().joinpath(".private-state").mkdir()

        records, errors = status.scan_current_records()

        self.assertEqual(
            [(record.internal_sequence, record.event_id) for record in records],
            [
                (valid.internal_sequence, "valid"),
                (dot_prefixed.internal_sequence, ".valid"),
            ],
        )
        messages = dict(errors)
        self.assertEqual(
            set(messages),
            {
                ".private-state",
                "malformed",
                "mismatched",
                "not-a-directory",
                "redirected",
            },
        )
        self.assertIn("status.json is missing or unsafe", messages[".private-state"])
        self.assertIn("malformed calculation record", messages["malformed"])
        self.assertIn("does not match", messages["mismatched"])
        self.assertIn("missing or unsafe", messages["not-a-directory"])
        self.assertIn("missing or unsafe", messages["redirected"])

    def test_current_record_scan_requires_manifest_identity(self) -> None:
        result = self._accept("manifest-mismatch")
        paths.events_dir().mkdir(parents=True)
        paths.queue_entry_dir(result.internal_sequence).rename(
            paths.event_service_dir("manifest-mismatch")
        )
        manifest_path = paths.event_service_dir("manifest-mismatch") / (
            "request-manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["internal_sequence"] += 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        records, errors = status.scan_current_records()

        self.assertEqual(records, [])
        self.assertEqual([name for name, _ in errors], ["manifest-mismatch"])
        self.assertIn("identity or schema", errors[0][1])

    def test_replaced_current_ancestor_is_rejected_for_all_record_access(self) -> None:
        result = self._accept("evt")
        paths.events_dir().mkdir(parents=True)
        paths.queue_entry_dir(result.internal_sequence).rename(
            paths.event_service_dir("evt")
        )
        displaced = paths.service_dir() / "displaced-events"
        paths.events_dir().rename(displaced)
        paths.events_dir().symlink_to(displaced, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            status.read_current_record("evt")
        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            status.update_current_record("evt", warnings=["unsafe"])
        records, errors = status.scan_current_records()

        self.assertEqual(records, [])
        self.assertEqual([name for name, _ in errors], ["<events>"])
        self.assertIn("unsafe service directory ancestry", errors[0][1])

    def test_replaced_queue_ancestor_is_rejected_for_all_record_access(self) -> None:
        result = self._accept("evt")
        displaced = paths.service_dir() / "displaced-queue"
        paths.queue_dir().rename(displaced)
        paths.queue_dir().symlink_to(displaced, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            status.read_status(result.internal_sequence)
        with self.assertRaisesRegex(ValueError, "missing or unsafe"):
            status.update_status(result.internal_sequence, warnings=["unsafe"])
        records, errors = status.scan_queue_records()

        self.assertEqual(records, [])
        self.assertEqual([name for name, _ in errors], ["<queue>"])
        self.assertIn("unsafe service directory ancestry", errors[0][1])

    def test_manifest_identity_and_schema_are_exact(self) -> None:
        cases = (
            (
                "extra-field",
                lambda manifest, result: manifest.update(extra=True),
                "fields",
            ),
            (
                "boolean-schema",
                lambda manifest, result: manifest.update(schema_version=True),
                "identity or schema",
            ),
            (
                "event-mismatch",
                lambda manifest, result: manifest.update(event_id="other"),
                "identity or schema",
            ),
            (
                "sequence-mismatch",
                lambda manifest, result: manifest.update(
                    internal_sequence=result.internal_sequence + 100
                ),
                "identity or schema",
            ),
        )
        expected_errors: dict[str, str] = {}
        for event_id, mutate, message in cases:
            result = self._accept(event_id)
            _, manifest = self._manifest(result.internal_sequence)
            mutate(manifest, result)
            self._store_manifest(result.internal_sequence, manifest)
            expected_errors[paths.queue_entry_name(result.internal_sequence)] = message
        valid = self._accept("valid")

        records, errors = self._scan_errors()

        self.assertEqual(
            [record.internal_sequence for record in records],
            [valid.internal_sequence],
        )
        self.assertEqual(set(errors), set(expected_errors))
        for entry, message in expected_errors.items():
            self.assertIn(message, errors[entry])

    def test_manifest_file_metadata_is_strictly_structural(self) -> None:
        def first_file(manifest: dict[str, Any]) -> dict[str, Any]:
            return manifest["files"][0]

        cases = (
            (
                "extra-item-field",
                lambda manifest: first_file(manifest).update(extra=True),
                "file entry",
            ),
            (
                "unsafe-basename",
                lambda manifest: first_file(manifest).update(basename="../event.xml"),
                "path separator",
            ),
            (
                "negative-size",
                lambda manifest: first_file(manifest).update(size_bytes=-1),
                "size is invalid",
            ),
            (
                "boolean-size",
                lambda manifest: first_file(manifest).update(size_bytes=True),
                "size is invalid",
            ),
            (
                "uppercase-digest",
                lambda manifest: first_file(manifest).update(sha256="A" * 64),
                "checksum is invalid",
            ),
            (
                "short-digest",
                lambda manifest: first_file(manifest).update(sha256="0" * 63),
                "checksum is invalid",
            ),
        )
        expected_errors: dict[str, str] = {}
        for event_id, mutate, message in cases:
            result = self._accept(event_id)
            _, manifest = self._manifest(result.internal_sequence)
            mutate(manifest)
            self._store_manifest(result.internal_sequence, manifest)
            expected_errors[paths.queue_entry_name(result.internal_sequence)] = message

        records, errors = self._scan_errors()

        self.assertEqual(records, [])
        self.assertEqual(set(errors), set(expected_errors))
        for entry, message in expected_errors.items():
            self.assertIn(message, errors[entry])

    def test_manifest_requires_unique_basenames_and_one_event_xml(self) -> None:
        duplicate = self._accept("duplicate-event")
        _, duplicate_manifest = self._manifest(duplicate.internal_sequence)
        duplicate_manifest["files"].append(dict(duplicate_manifest["files"][0]))
        self._store_manifest(duplicate.internal_sequence, duplicate_manifest)

        missing = self._accept("missing-event")
        _, missing_manifest = self._manifest(missing.internal_sequence)
        missing_manifest["files"] = []
        self._store_manifest(missing.internal_sequence, missing_manifest)

        records, errors = self._scan_errors()

        self.assertEqual(records, [])
        self.assertIn(
            "duplicate basename",
            errors[paths.queue_entry_name(duplicate.internal_sequence)],
        )
        self.assertIn(
            "exactly one event.xml",
            errors[paths.queue_entry_name(missing.internal_sequence)],
        )

    def test_snapshot_requires_exact_regular_non_symlink_manifest_entries(self) -> None:
        nonregular = self._accept_directory("nonregular", "extra.bin")
        nonregular_entry = paths.queue_request_dir(nonregular.internal_sequence)
        nonregular_entry.joinpath("extra.bin").unlink()
        nonregular_entry.joinpath("extra.bin").mkdir()

        symlinked = self._accept_directory("symlinked", "extra.bin")
        symlinked_entry = paths.queue_request_dir(symlinked.internal_sequence)
        symlinked_entry.joinpath("extra.bin").unlink()
        symlinked_entry.joinpath("extra.bin").symlink_to("event.xml")

        unmanifested = self._accept("unmanifested")
        paths.queue_request_dir(unmanifested.internal_sequence).joinpath(
            "extra.bin"
        ).write_bytes(b"extra")

        missing = self._accept_directory("missing-snapshot", "extra.bin")
        paths.queue_request_dir(missing.internal_sequence).joinpath(
            "extra.bin"
        ).unlink()

        records, errors = self._scan_errors()

        self.assertEqual(records, [])
        self.assertIn(
            "non-regular entry",
            errors[paths.queue_entry_name(nonregular.internal_sequence)],
        )
        self.assertIn(
            "non-regular entry",
            errors[paths.queue_entry_name(symlinked.internal_sequence)],
        )
        self.assertIn(
            "unmanifested entries",
            errors[paths.queue_entry_name(unmanifested.internal_sequence)],
        )
        self.assertIn(
            "missing manifest entries",
            errors[paths.queue_entry_name(missing.internal_sequence)],
        )

    def test_snapshot_size_must_match_manifest_metadata(self) -> None:
        result = self._accept()
        _, manifest = self._manifest(result.internal_sequence)
        manifest["files"][0]["size_bytes"] += 1
        self._store_manifest(result.internal_sequence, manifest)

        records, errors = self._scan_errors()

        self.assertEqual(records, [])
        self.assertIn(
            "size differs",
            errors[paths.queue_entry_name(result.internal_sequence)],
        )

    def test_discovery_does_not_open_snapshot_file_contents(self) -> None:
        directory = paths.event_input_dir("large")
        directory.mkdir(parents=True)
        with directory.joinpath("event.xml").open("wb") as stream:
            stream.truncate(8 * 1024 * 1024)
        result = accept_request("large")
        real_open = status.os.open

        def reject_snapshot_open(file: object, *args: object, **kwargs: object) -> int:
            if file == "event.xml":
                raise AssertionError("queue discovery opened accepted request bytes")
            return real_open(file, *args, **kwargs)

        with mock.patch.object(status.os, "open", side_effect=reject_snapshot_open):
            records, errors = scan_queue_records()

        self.assertEqual(
            [record.internal_sequence for record in records],
            [result.internal_sequence],
        )
        self.assertEqual(errors, [])

    def test_only_four_lifecycle_states_are_accepted(self) -> None:
        self.assertEqual(
            {state.value for state in LifecycleState},
            {"QUEUED", "RUNNING", "SUCCESS", "FAILED"},
        )
        result = self._accept()
        target = paths.queue_status_file(result.internal_sequence)
        data = json.loads(target.read_text(encoding="utf-8"))
        data["status"] = "CANCELLED"
        target.write_text(json.dumps(data), encoding="utf-8")

        records, malformed = scan_queue_records()

        self.assertEqual(records, [])
        self.assertEqual(len(malformed), 1)
        self.assertIn("invalid lifecycle state", malformed[0][1])


if __name__ == "__main__":
    unittest.main()
