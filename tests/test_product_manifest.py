from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import paths, product_manifest, product_validation, status
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


class ProductManifestTests(unittest.TestCase):
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

    def _products(self, event_id: str) -> Path:
        products = paths.event_native_products_dir(event_id)
        products.mkdir(parents=True)
        return products

    @staticmethod
    def _validation(
        *,
        passed: bool = True,
    ) -> product_validation.ProductValidationResult:
        checks = (
            product_validation.RequiredProductCheck(
                path="core.bin",
                size=4 if passed else None,
                passed=passed,
                reason="generic checks passed" if passed else "product is missing",
            ),
            product_validation.RequiredProductCheck(
                path="core.bin",
                size=4 if passed else None,
                passed=passed,
                reason="generic checks passed" if passed else "product is missing",
            ),
        )
        return product_validation.ProductValidationResult(
            required_paths=("core.bin", "core.bin"),
            source="configured",
            checks=checks,
            passed=passed,
        )

    @staticmethod
    def _sha256(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    def test_complete_manifest_has_exact_stable_inventory_and_evidence(
        self,
    ) -> None:
        record = self._promote("complete")
        products = self._products("complete")
        nested = products / "nested"
        nested.mkdir()
        payloads = {
            "core.bin": b"core",
            "nested/a.txt": b"a",
            "nested/z.bin": b"z" * 17,
        }
        for relative, payload in payloads.items():
            product = products / relative
            product.parent.mkdir(parents=True, exist_ok=True)
            product.write_bytes(payload)
        outside = Path(self.temporary.name) / "outside.bin"
        outside.write_bytes(b"outside")
        (products / "linked.bin").symlink_to(outside)
        validation = self._validation()
        status_before = paths.event_status_file("complete").read_bytes()
        products_before = {
            path: path.read_bytes()
            for path in products.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

        with mock.patch.object(
            product_validation,
            "validate_required_products",
            side_effect=AssertionError("product revalidation attempted"),
        ):
            manifest_file = product_manifest.publish_product_manifest(
                record,
                validation,
            )

        self.assertEqual(manifest_file, paths.event_manifest_file("complete"))
        self.assertEqual(
            json.loads(manifest_file.read_text(encoding="utf-8")),
            {
                "event_id": "complete",
                "internal_sequence": record.internal_sequence,
                "partial": False,
                "primary_reason": None,
                "inventory_failures": [],
                "required_products": {
                    "paths": ["core.bin", "core.bin"],
                    "source": "configured",
                    "passed": True,
                    "checks": [
                        {
                            "path": "core.bin",
                            "size": 4,
                            "passed": True,
                            "reason": "generic checks passed",
                        },
                        {
                            "path": "core.bin",
                            "size": 4,
                            "passed": True,
                            "reason": "generic checks passed",
                        },
                    ],
                },
                "products": [
                    {
                        "path": relative,
                        "size": len(payload),
                        "sha256": self._sha256(payload),
                    }
                    for relative, payload in sorted(payloads.items())
                ],
            },
        )
        self.assertEqual(
            stat.S_IMODE(manifest_file.stat().st_mode) & 0o077,
            0,
        )
        self.assertEqual(paths.event_status_file("complete").read_bytes(), status_before)
        self.assertEqual(
            {
                path: path.read_bytes()
                for path in products.rglob("*")
                if path.is_file() and not path.is_symlink()
            },
            products_before,
        )
        self.assertEqual(
            list(paths.event_service_dir("complete").glob(".product-manifest-*.tmp")),
            [],
        )

    def test_hashing_streams_bounded_chunks(self) -> None:
        record = self._promote("streaming")
        products = self._products("streaming")
        payload = b"x" * (product_manifest.HASH_CHUNK_SIZE * 2 + 13)
        product = products / "core.bin"
        product.write_bytes(payload)
        real_open = Path.open
        read_sizes: list[int] = []

        class TrackingReader:
            def __init__(self, stream) -> None:
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size: int = -1) -> bytes:
                read_sizes.append(size)
                return self.stream.read(size)

        def tracked_open(path: Path, mode: str = "r", *args, **kwargs):
            opened = real_open(path, mode, *args, **kwargs)
            if path == product and mode == "rb":
                return TrackingReader(opened)
            return opened

        with mock.patch.object(Path, "open", new=tracked_open):
            product_manifest.publish_product_manifest(
                record,
                self._validation(),
            )

        self.assertEqual(
            read_sizes,
            [product_manifest.HASH_CHUNK_SIZE] * 4,
        )

    def test_partial_manifest_keeps_primary_and_secondary_inventory_failures(
        self,
    ) -> None:
        record = self._promote("partial")
        products = self._products("partial")
        (products / "core.bin").write_bytes(b"core")
        unreadable = products / "unreadable.bin"
        unreadable.write_bytes(b"blocked")
        real_open = Path.open

        def controlled_open(path: Path, mode: str = "r", *args, **kwargs):
            if path == unreadable and mode == "rb":
                raise PermissionError("injected read failure")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", new=controlled_open):
            manifest_file = product_manifest.publish_product_manifest(
                record,
                self._validation(passed=False),
                primary_reason="required product validation failed",
            )

        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertTrue(payload["partial"])
        self.assertEqual(
            payload["primary_reason"],
            "required product validation failed",
        )
        self.assertEqual(
            payload["inventory_failures"],
            [
                {
                    "path": "unreadable.bin",
                    "reason": "PermissionError: injected read failure",
                }
            ],
        )
        self.assertEqual(
            payload["products"],
            [
                {
                    "path": "core.bin",
                    "size": 4,
                    "sha256": self._sha256(b"core"),
                }
            ],
        )
        self.assertFalse(payload["required_products"]["passed"])

    def test_partial_manifest_records_missing_inventory_as_secondary(self) -> None:
        record = self._promote("missing-products")

        manifest_file = product_manifest.publish_product_manifest(
            record,
            self._validation(passed=False),
            primary_reason="native execution failed",
        )

        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["primary_reason"], "native execution failed")
        self.assertEqual(payload["products"], [])
        self.assertEqual(payload["inventory_failures"][0]["path"], ".")
        self.assertIn("FileNotFoundError", payload["inventory_failures"][0]["reason"])

    def test_partial_manifest_supports_pre_resolution_failure(self) -> None:
        record = self._promote("pre-resolution")
        products = self._products("pre-resolution")
        (products / "native.txt").write_bytes(b"native")

        manifest_file = product_manifest.publish_product_manifest(
            record,
            None,
            primary_reason="profile preparation failed",
        )

        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["primary_reason"], "profile preparation failed")
        self.assertIsNone(payload["required_products"])
        self.assertEqual(
            payload["products"],
            [
                {
                    "path": "native.txt",
                    "size": 6,
                    "sha256": self._sha256(b"native"),
                }
            ],
        )

        manifest_file = product_manifest.publish_product_manifest(
            record,
            self._validation(),
            primary_reason="later service finalization failed",
        )
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertTrue(payload["required_products"]["passed"])

    def test_complete_inventory_failure_propagates_without_publication(self) -> None:
        record = self._promote("complete-failure")
        products = self._products("complete-failure")
        unreadable = products / "core.bin"
        unreadable.write_bytes(b"core")
        real_open = Path.open

        def controlled_open(path: Path, mode: str = "r", *args, **kwargs):
            if path == unreadable and mode == "rb":
                raise PermissionError("injected read failure")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", new=controlled_open):
            with self.assertRaisesRegex(
                product_manifest.ProductManifestError,
                "injected read failure",
            ):
                product_manifest.publish_product_manifest(
                    record,
                    self._validation(),
                )

        self.assertFalse(paths.event_manifest_file("complete-failure").exists())

    def test_requires_matching_current_record_and_valid_mode(self) -> None:
        queued = self._accept("queued")
        queued_record = status.transition_to_running(queued.internal_sequence)
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            product_manifest.publish_product_manifest(
                queued_record,
                self._validation(),
            )

        self._promote("identity")
        later = self._accept("identity")
        later_record = status.transition_to_running(later.internal_sequence)
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            product_manifest.publish_product_manifest(
                later_record,
                self._validation(),
            )

        current = self._promote("invalid-mode")
        invalid_validation = self._validation(passed=False)
        with self.assertRaisesRegex(ValueError, "requires an all-pass"):
            product_manifest.publish_product_manifest(
                current,
                invalid_validation,
            )
        with self.assertRaisesRegex(ValueError, "requires an all-pass"):
            product_manifest.publish_product_manifest(
                current,
                None,
            )
        for primary_reason in ("", 1):
            with self.subTest(primary_reason=primary_reason):
                with self.assertRaisesRegex(ValueError, "nonempty string"):
                    product_manifest.publish_product_manifest(
                        current,
                        invalid_validation,
                        primary_reason=primary_reason,  # type: ignore[arg-type]
                    )
        self.assertFalse(paths.event_manifest_file("invalid-mode").exists())

    def test_rejects_terminal_supplied_or_authoritative_current_record(
        self,
    ) -> None:
        supplied_running = self._promote("terminal-current")
        terminal_current = status.transition_current_record(
            "terminal-current",
            status.LifecycleState.FAILED,
            failure={"code": "test_failure", "message": "fixture"},
            service_outcome={"completed": True, "successful": False},
        )

        with self.assertRaisesRegex(ValueError, "current.*RUNNING"):
            product_manifest.publish_product_manifest(
                supplied_running,
                self._validation(),
            )
        with self.assertRaisesRegex(ValueError, "supplied.*RUNNING"):
            product_manifest.publish_product_manifest(
                terminal_current,
                self._validation(),
            )
        self.assertFalse(paths.event_manifest_file("terminal-current").exists())

    def test_atomic_write_failures_preserve_preceding_manifest(self) -> None:
        record = self._promote("atomic")
        products = self._products("atomic")
        (products / "core.bin").write_bytes(b"core")
        manifest_file = product_manifest.publish_product_manifest(
            record,
            self._validation(),
        )
        preceding = manifest_file.read_bytes()

        with mock.patch.object(
            product_manifest.os,
            "write",
            side_effect=OSError("injected write failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                product_manifest.publish_product_manifest(
                    record,
                    self._validation(),
                )

        self.assertEqual(manifest_file.read_bytes(), preceding)
        self.assertEqual(
            list(paths.event_service_dir("atomic").glob(".product-manifest-*.tmp")),
            [],
        )

    def test_publication_fsyncs_file_and_directory(self) -> None:
        record = self._promote("durable")
        products = self._products("durable")
        (products / "core.bin").write_bytes(b"core")

        with mock.patch.object(
            product_manifest.os,
            "fsync",
            wraps=os.fsync,
        ) as fsync_call:
            product_manifest.publish_product_manifest(
                record,
                self._validation(),
            )

        self.assertEqual(fsync_call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
