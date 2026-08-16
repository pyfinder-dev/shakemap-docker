from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from shakemap_service import cli, directory_access, main, paths, runner, worker
from shakemap_service.config import Settings
from shakemap_service.request_validation import (
    validate_configuration_name,
    validate_event_id,
    validate_upload_basename,
)
from shakemap_service.worker import execute_shakemap, run_worker_cycle


class RuntimePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_settings = paths.settings
        paths.settings = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )

    def tearDown(self) -> None:
        paths.settings = self.previous_settings
        self.temporary.cleanup()

    def test_paths_use_only_the_canonical_runtime_namespaces(self) -> None:
        root = Path(self.temporary.name) / "shakemap"
        self.assertEqual(paths.inputs_dir(), root / "data" / "inputs")
        self.assertEqual(paths.event_input_dir("evt"), root / "data" / "inputs" / "evt")
        self.assertEqual(
            paths.queue_entry_dir(7),
            root / ".service" / "queue" / "00000000000000000007",
        )
        self.assertEqual(paths.event_service_dir("evt"), root / ".service" / "events" / "evt")
        self.assertEqual(
            paths.event_archive_dir("evt", "20260815T120000.000000Z"),
            root / ".service" / "archive" / "evt-20260815T120000.000000Z",
        )
        self.assertEqual(paths.event_products_dir("evt"), root / "products" / "evt")

        rendered = {str(path.relative_to(root)) for path in paths.all_service_dirs()}
        self.assertEqual(
            rendered,
            {
                "products",
                "logs",
                "data",
                "data/inputs",
                ".service/queue",
                ".service/events",
                ".service/archive",
            },
        )
        forbidden = {
            "incoming",
            "events",
            "work",
            "archive",
            "preparation",
            ".service/work",
            ".service/preparation",
        }
        self.assertTrue(rendered.isdisjoint(forbidden))

    def test_service_records_are_separate_from_native_products(self) -> None:
        product_root = paths.event_products_dir("evt")
        service_root = paths.event_service_dir("evt")
        for service_path in (
            paths.event_request_dir("evt"),
            paths.event_profile_dir("evt"),
            paths.event_status_file("evt"),
            paths.event_provenance_file("evt"),
            paths.event_manifest_file("evt"),
            paths.event_logs_dir("evt"),
        ):
            self.assertTrue(service_path.is_relative_to(service_root))
            self.assertFalse(service_path.is_relative_to(product_root))

    def test_directory_access_creates_missing_components_and_reopens_normally(self) -> None:
        with self.assertRaises(FileNotFoundError):
            directory_access.open_service_directory(paths.queue_dir(), create=False)
        service_root = paths.service_root()
        service_root.mkdir()
        marker = service_root / "operator-file"
        marker.write_bytes(b"preserve")
        with mock.patch.object(
            directory_access.os, "fsync", wraps=os.fsync
        ) as synced:
            handle = directory_access.open_service_directory(paths.queue_dir(), create=True)
        handle.close()
        self.assertEqual(synced.call_count, 2)
        self.assertEqual(marker.read_bytes(), b"preserve")
        reopened = directory_access.open_service_directory(paths.queue_dir(), create=False)
        self.assertTrue(stat.S_ISDIR(os.fstat(reopened.descriptor).st_mode))
        reopened.close()

    def test_directory_access_rejects_outside_links_and_non_directories(self) -> None:
        root = Path(self.temporary.name)
        real = root / "real"
        (real / "child").mkdir(parents=True)
        ancestor_link = root / "ancestor-link"
        ancestor_link.symlink_to(real, target_is_directory=True)
        final_link = root / "final-link"
        final_link.symlink_to(real / "child", target_is_directory=True)
        ancestor_file = root / "ancestor-file"
        ancestor_file.write_bytes(b"file")
        parent = root / "parent"
        parent.mkdir()
        final_file = parent / "final-file"
        final_file.write_bytes(b"file")
        outside = root.parent / f"{root.name}-outside"
        cases = (
            (outside, True, "outside the configured runtime"),
            (root / ".." / outside.name, True, "outside the configured runtime"),
            (ancestor_link / "child", False, "unsafe service directory"),
            (final_link, False, "unsafe service directory"),
            (ancestor_file / "child", True, "unsafe service directory"),
            (final_file, True, "unsafe service directory"),
        )
        for target, create, message in cases:
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, message):
                    directory_access.open_service_directory(target, create=create)

    def test_event_ids_preserve_safe_punctuation_and_unicode(self) -> None:
        values = ["quake:alpha+1", "σεισμός 01", "A.b_c@2026", "é", "a\\b"]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(validate_event_id(value), value)
                self.assertEqual(paths.event_input_dir(value).name, value)

    def test_event_ids_reject_only_representation_hazards(self) -> None:
        for value in ("", ".", "..", "a/b", "bad\x00id", "line\nbreak", "-option"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_event_id(value)
        with self.assertRaises(ValueError):
            validate_event_id("a" * 256)

    def test_event_id_length_uses_utf8_bytes_for_archive_safe_names(self) -> None:
        self.assertEqual(validate_event_id("a" * 231), "a" * 231)
        with self.assertRaisesRegex(
            ValueError,
            "uses 232 UTF-8 bytes; shorten it to at most 231 bytes",
        ):
            validate_event_id("a" * 232)

        multibyte_boundary = "a" + ("é" * 115)
        self.assertEqual(len(multibyte_boundary.encode("utf-8")), 231)
        self.assertEqual(validate_event_id(multibyte_boundary), multibyte_boundary)
        with self.assertRaisesRegex(ValueError, "uses 232 UTF-8 bytes"):
            validate_event_id("é" * 116)

    def test_configuration_validation_is_lookup_safety_only(self) -> None:
        self.assertEqual(validate_configuration_name("regional set+1"), "regional set+1")
        self.assertEqual(validate_configuration_name("regional\\set"), "regional\\set")
        for value in ("", ".", "..", "a/b", "line\nbreak"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_configuration_name(value)

    def test_upload_basename_preserves_posix_backslash(self) -> None:
        self.assertEqual(validate_upload_basename("native\\file.ext"), "native\\file.ext")

    def test_native_worker_boundary_delegates_but_does_not_claim_work(self) -> None:
        result = run_worker_cycle()
        self.assertFalse(result.claimed)
        self.assertEqual(result.outcome, "worker_disabled")
        record = object()
        with (
            mock.patch.dict(os.environ, {"M5J_TEST": "caller"}, clear=True),
            mock.patch.object(
                worker.calculation,
                "execute_calculation",
                return_value="FAILED",
            ) as delegated,
        ):
            self.assertEqual(execute_shakemap(record), "FAILED")
            passed_environment = delegated.call_args.kwargs["base_environment"]
            self.assertEqual(passed_environment, {"M5J_TEST": "caller"})
            self.assertIsNot(passed_environment, os.environ)
        delegated.assert_called_once_with(
            record,
            base_environment={"M5J_TEST": "caller"},
        )

    def test_calculation_routes_return_the_same_disabled_response(self) -> None:
        with TestClient(main.app) as client:
            responses = (
                client.post("/events/submit"),
                client.get("/events"),
                client.get("/events/evt"),
                client.get("/events/evt/products"),
                client.get("/queue"),
            )
        expected_body = responses[0].json()
        for response in responses:
            with self.subTest(response=response):
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), expected_body)

    def test_public_client_remains_disabled(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]), 2)

    def test_managed_native_entry_points_are_inert(self) -> None:
        with self.assertRaisesRegex(runner.ShakeError, "materialization is disabled"):
            runner.materialize_calculation(object())
        with self.assertRaisesRegex(runner.ShakeError, "execution is disabled"):
            runner.run_shake_for_event(object())
        self.assertFalse(paths.products_dir().exists())

if __name__ == "__main__":
    unittest.main()
