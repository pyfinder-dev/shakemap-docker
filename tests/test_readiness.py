"""Host tests for durable, identity-bound readiness state."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import paths, preparation, readiness
from shakemap_service.config import Settings


def service_identity() -> dict[str, object]:
    return {
        "immutable_image": {
            "available": True,
            "upstream": {"release_tag": "v4.4.9", "source_commit": "a" * 40},
            "installed": {"shakemap_distribution_version": "4.4.9"},
        },
        "deployment": {"available": True, "image_id": "sha256:" + "b" * 64},
    }


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = paths.settings
        paths.settings = Settings(runtime_root=self.temporary.name)
        self.identity = service_identity()

    def tearDown(self) -> None:
        paths.settings = self.previous
        self.temporary.cleanup()

    def test_missing_not_ready_ready_and_identity_mismatches(self) -> None:
        self.assertEqual(
            readiness.read_readiness(),
            {"ready": False, "reason": readiness.NOT_RECORDED},
        )
        with mock.patch.object(readiness, "_current_identity", side_effect=AssertionError):
            readiness._record_not_ready("data activation failed")
            self.assertEqual(
                readiness.read_readiness(),
                {"ready": False, "reason": "data activation failed"},
            )
        with (
            mock.patch.object(preparation, "validate_pinned_global_assets") as validation,
            mock.patch.object(preparation, "validate_pinned_file") as file_validation,
            mock.patch.object(preparation, "sha256") as hashing,
            mock.patch("shakemap_service.runner.run_shake") as native,
        ):
            readiness._record_ready(self.identity)
            self.assertEqual(
                readiness.read_readiness(self.identity),
                {"ready": True, "reason": None},
            )
        validation.assert_not_called()
        file_validation.assert_not_called()
        hashing.assert_not_called()
        native.assert_not_called()
        with mock.patch(
            "shakemap_service.build_identity.service_identity",
            return_value=self.identity,
        ) as loader:
            self.assertTrue(readiness.read_readiness()["ready"])
            loader.assert_called_once_with()

        changes = (
            lambda value: value["deployment"].update(image_id="sha256:" + "c" * 64),
            lambda value: value["immutable_image"]["upstream"].update(
                release_tag="v4.4.10"
            ),
            lambda value: value["immutable_image"]["upstream"].update(
                source_commit="d" * 40
            ),
            lambda value: value["immutable_image"]["installed"].update(
                shakemap_distribution_version="4.4.10"
            ),
            lambda value: value["deployment"].update(image_id="sha256:" + "A" * 64),
            lambda value: value["immutable_image"]["upstream"].update(
                source_commit="A" * 40
            ),
        )
        for change in changes:
            changed = copy.deepcopy(self.identity)
            change(changed)
            self.assertEqual(
                readiness.read_readiness(changed)["reason"], readiness.MISMATCH
            )
        for name in ("vs30", "topography"):
            with mock.patch.dict(preparation.GLOBAL_ASSETS[name], {"size": 1}):
                self.assertEqual(
                    readiness.read_readiness(self.identity)["reason"],
                    readiness.MISMATCH,
                )
        with mock.patch.dict(
            preparation.GLOBAL_ASSETS["vs30"], {"relative": "../unsafe"}
        ):
            self.assertEqual(
                readiness.read_readiness(self.identity)["reason"], readiness.MISMATCH
            )

    def test_strict_stored_identity_validation(self) -> None:
        readiness._record_ready(self.identity)
        record = paths.service_dir() / readiness.RECORD_NAME
        original = json.loads(record.read_text(encoding="utf-8"))
        invalid = (
            ("image_id", "b" * 64),
            ("image_id", "sha256:" + "b" * 63),
            ("image_id", "sha256:" + "A" * 64),
            ("source_commit", "a" * 39),
            ("source_commit", "A" * 40),
            ("source_commit", "g" * 40),
            ("release_tag", ""),
            ("release_tag", "not-a-final-release"),
            ("release_tag", "v4.4.9rc1"),
            ("release_tag", "v04.4.9"),
            ("shakemap_version", ""),
        )
        for name, value in invalid:
            changed = copy.deepcopy(original)
            changed["identity"][name] = value
            record.write_text(json.dumps(changed), encoding="utf-8")
            self.assertEqual(
                readiness.read_readiness()["reason"], readiness.UNAVAILABLE
            )
        for value in (
            "",
            ".",
            "..",
            "/absolute",
            "a/",
            "a//b",
            "a/./b",
            "a/../b",
            "a\\b",
        ):
            changed = copy.deepcopy(original)
            changed["identity"]["global_assets"]["vs30"]["relative"] = value
            record.write_text(json.dumps(changed), encoding="utf-8")
            self.assertEqual(
                readiness.read_readiness()["reason"], readiness.UNAVAILABLE
            )
        for value in ("a" * 63, "A" * 64, "g" * 64):
            changed = copy.deepcopy(original)
            changed["identity"]["global_assets"]["topography"]["sha256"] = value
            record.write_text(json.dumps(changed), encoding="utf-8")
            self.assertEqual(
                readiness.read_readiness()["reason"], readiness.UNAVAILABLE
            )

    def test_malformed_oversized_and_unsafe_records_fail_closed(self) -> None:
        service = paths.service_dir()
        service.mkdir(parents=True)
        record = service / readiness.RECORD_NAME
        bad_values = (
            b"{",
            json.dumps(
                {
                    "schema_version": 2,
                    "state": "not_ready",
                    "reason": "bad",
                    "identity": None,
                }
            ).encode(),
            b"x" * (readiness.MAX_RECORD_BYTES + 1),
        )
        for payload in bad_values:
            record.write_bytes(payload)
            self.assertEqual(
                readiness.read_readiness()["reason"], readiness.UNAVAILABLE
            )

        target = service / "target"
        target.write_text("{}", encoding="utf-8")
        for kind in ("directory", "symlink"):
            record.unlink(missing_ok=True)
            record.mkdir() if kind == "directory" else record.symlink_to(target)
            self.assertEqual(
                readiness.read_readiness()["reason"], readiness.UNAVAILABLE
            )
            with self.assertRaises((OSError, ValueError)):
                readiness._record_not_ready("must not replace unsafe state")
            record.rmdir() if kind == "directory" else record.unlink()

        os.mkfifo(record)
        self.assertEqual(
            readiness.read_readiness()["reason"], readiness.UNAVAILABLE
        )
        with self.assertRaises((OSError, ValueError)):
            readiness._record_not_ready("must not replace fifo")
        record.unlink()

        with tempfile.TemporaryDirectory() as unsafe:
            outside = Path(unsafe) / "outside"
            outside.mkdir()
            root = Path(unsafe) / "runtime"
            root.mkdir()
            (root / "shakemap").symlink_to(outside, target_is_directory=True)
            paths.settings = Settings(runtime_root=str(root))
            self.assertEqual(
                readiness.read_readiness()["reason"], readiness.UNAVAILABLE
            )

    def test_failed_replacement_preserves_record_and_removes_temporary(self) -> None:
        readiness._record_not_ready("old reason")
        record = paths.service_dir() / readiness.RECORD_NAME
        before = record.read_bytes()
        with self.assertRaises(ValueError):
            readiness._record_not_ready("x" * readiness.MAX_RECORD_BYTES)
        self.assertEqual(record.read_bytes(), before)
        with mock.patch.object(readiness.os, "replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                readiness._record_not_ready("new reason")
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(list(paths.service_dir().glob(".readiness.json.*.tmp")), [])
