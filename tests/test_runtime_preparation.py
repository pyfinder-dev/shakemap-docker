#!/usr/bin/env python3
"""Host-side safety tests for build-before-prepare-before-start workflow."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from shakemap_service import preparation


HDF = b"\x89HDF\r\n\x1a\n" + b"scientific-grid"


def spec(data: bytes = HDF) -> dict:
    return {
        "url": "https://example.test/grid.grd",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "checksum_authority": "test source checksum",
    }


class FileProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_existing_file_is_reused_without_download(self) -> None:
        target = self.root / "global.grd"
        target.write_bytes(HDF)
        with patch.object(preparation, "download", side_effect=AssertionError("download called")):
            result = preparation.provision_file(target, spec(), None, True)
        self.assertEqual(result["action"], "reused")
        self.assertEqual(target.read_bytes(), HDF)

    def test_manual_import_uses_atomic_sibling_and_preserves_validity(self) -> None:
        source = self.root / "manual.grd"
        source.write_bytes(HDF)
        target = self.root / "data/global.grd"
        result = preparation.provision_file(target, spec(), source, False)
        self.assertEqual(result["action"], "imported")
        self.assertEqual(target.read_bytes(), HDF)
        self.assertEqual(list(target.parent.glob(".global.grd.prepare-*")), [])

    def test_invalid_operator_file_is_preserved_after_valid_replacement(self) -> None:
        source = self.root / "manual.grd"
        source.write_bytes(HDF)
        target = self.root / "data/global.grd"
        target.parent.mkdir()
        target.write_bytes(b"partial")
        result = preparation.provision_file(target, spec(), source, False)
        preserved = Path(result["preserved_invalid_path"])
        self.assertEqual(preserved.read_bytes(), b"partial")
        self.assertEqual(target.read_bytes(), HDF)

    def test_missing_data_fails_when_download_disabled(self) -> None:
        with self.assertRaisesRegex(preparation.PreparationError, "download disabled"):
            preparation.provision_file(self.root / "missing.grd", spec(), None, False)


class SlabAndManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manual_slab_archive_is_verified_extracted_and_reused(self) -> None:
        archive = self.root / "slab2.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for suffix in ("dep", "dip", "str", "unc"):
                bundle.writestr(f"tst_slab2_{suffix}_01.01.01.grd", HDF + suffix.encode())
        slab_spec = {
            "url": "https://example.test/slab2.zip",
            "size": archive.stat().st_size,
            "sha256": preparation.sha256(archive),
            "checksum_authority": "test",
            "file_count": 4,
        }
        with patch.object(preparation, "SLAB2", slab_spec):
            first = preparation.provision_slabs(self.root, archive, False)
            second = preparation.provision_slabs(self.root, None, False)
        self.assertEqual(first["action"], "imported")
        self.assertEqual(second["action"], "reused")

    def test_preparation_record_requires_base_snapshot(self) -> None:
        service = self.root / "shakemap"
        manifest = service / ".service/preparation/manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "run_id": "test",
            "identity": {},
            "permissions": {},
            "grids": {},
            "base_path": "/container/path",
            "ready": True,
        }), encoding="utf-8")
        state = preparation.load_preparation(service)
        self.assertFalse(state["ready"])
        self.assertIn("base snapshot is missing", state["reason"])

    def test_preparation_record_requires_exact_image_identity(self) -> None:
        service = self.root / "shakemap"
        manifest = service / ".service/preparation/manifest.json"
        manifest.parent.mkdir(parents=True)
        (service / ".service/preparation/base/global").mkdir(parents=True)
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "run_id": "test",
            "identity": {"available": True, "built_at_utc": "prepared-image"},
            "permissions": {},
            "grids": {},
            "base_path": "/container/path",
            "ready": True,
        }), encoding="utf-8")
        with patch(
            "shakemap_service.build_identity.load_build_identity",
            return_value={"immutable_image": {"available": True, "built_at_utc": "different-image"}},
        ):
            state = preparation.load_preparation(service)
        self.assertFalse(state["ready"])
        self.assertIn("identity does not match", state["reason"])

    def test_host_permission_check_covers_contract_layout(self) -> None:
        evidence = preparation.ensure_host_permissions(self.root)
        paths = {Path(item["path"]).relative_to(self.root).as_posix() for item in evidence["checks"]}
        self.assertEqual(paths, {
            "shakemap/incoming", "shakemap/products", "shakemap/logs", "shakemap/data",
            "shakemap/.service/events", "shakemap/.service/work", "shakemap/.service/archive",
            "shakemap/.service/preparation",
        })


class LifecycleSourceTests(unittest.TestCase):
    def test_module_order_parser_accepts_release_logging_formats(self) -> None:
        semicolon = "INFO;date;shake.main;Running command select\nINFO;date;shake.main;Finished running command select: Elapsed 1 sec\n"
        default = "INFO -- date -- shake.main -- Running command model\nINFO -- date -- shake.main -- Finished running command model: Elapsed 1 sec\n"
        self.assertEqual(preparation.parse_module_order(semicolon), (["select"], ["select"]))
        self.assertEqual(preparation.parse_module_order(default), (["model"], ["model"]))

    def test_operator_workflow_has_no_docker_exec_or_uniform_vs30(self) -> None:
        project = Path(__file__).resolve().parents[1]
        files = [
            project / "scripts/configure-shakemap.sh",
            project / "scripts/start-shakemap-docker.sh",
            project / "entrypoint.sh",
        ]
        text = "\n".join(path.read_text(encoding="utf-8") for path in files)
        self.assertNotIn("docker exec", text)
        self.assertNotIn("SHAKEMAP_ALLOW_UNIFORM_VS30", text)
        self.assertIn("shakemap_service.preparation", text)

    def test_obsolete_lifecycle_scripts_are_removed(self) -> None:
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        for name in (
            "verify-shakemap-build.sh", "verify-shakemap-config.sh",
            "run-shakemap-ci-tests.sh", "inspect-shakemap-config.sh",
            "inspect-shakemap-events.sh",
        ):
            self.assertFalse((scripts / name).exists(), name)

    def test_natural_earth_manifest_is_complete_and_checksum_pinned(self) -> None:
        path = Path(__file__).resolve().parents[1] / "image-support/natural-earth-v5.1.2.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["commit"], "f1890d9f152c896d250a77557a5751a93d494776")
        self.assertEqual(len(manifest["files"]), 20)
        self.assertEqual(len({item["target_path"] for item in manifest["files"]}), 20)
        self.assertTrue(all(len(item["sha256"]) == 64 and item["size"] > 0 for item in manifest["files"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
