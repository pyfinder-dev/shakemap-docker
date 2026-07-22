#!/usr/bin/env python3
"""Host-side safety tests for build-before-prepare-before-start workflow."""

from __future__ import annotations

import hashlib
import base64
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
VALID_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAMAAoDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iiigD//2Q=="
)


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
            "schema_version": 2,
            "run_id": "test",
            "identity": {},
            "permissions": {},
            "grids": {},
            "base_path": "/container/path",
            "california_verification": {"product_validation": {"passed": True}},
            "global_verification": {"product_validation": {"passed": True}},
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
            "schema_version": 2,
            "run_id": "test",
            "identity": {"available": True, "built_at_utc": "prepared-image"},
            "permissions": {},
            "grids": {},
            "base_path": "/container/path",
            "california_verification": {"product_validation": {"passed": True}},
            "global_verification": {"product_validation": {"passed": True}},
            "ready": True,
        }), encoding="utf-8")
        with patch(
            "shakemap_service.build_identity.load_build_identity",
            return_value={"immutable_image": {"available": True, "built_at_utc": "different-image"}},
        ):
            state = preparation.load_preparation(service)
        self.assertFalse(state["ready"])
        self.assertIn("identity does not match", state["reason"])

    def test_failed_product_validation_keeps_preparation_not_ready(self) -> None:
        service = self.root / "shakemap"
        manifest = service / ".service/preparation/manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({
            "schema_version": 2,
            "run_id": "failed-products",
            "ready": False,
            "error": "ProductValidationError: intensity.jpg is a legend-only strip",
            "failed_product_validation": {
                "passed": False,
                "errors": ["intensity.jpg: image aspect is incompatible"],
            },
        }), encoding="utf-8")
        state = preparation.load_preparation(service)
        self.assertFalse(state["ready"])
        self.assertIn("ProductValidationError", state["reason"])

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

    def test_operator_workflow_has_portable_python_prerequisite(self) -> None:
        project = Path(__file__).resolve().parents[1]
        paths = [
            project / "README.md",
            project / "docs/quick-start.md",
            project / "scripts/README.md",
            project / "scripts/build-shakemap-docker.sh",
            project / "scripts/configure-shakemap.sh",
            project / "scripts/start-shakemap-docker.sh",
            project / "scripts/verify-shakemap-deployment.sh",
        ]
        text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        self.assertNotIn("../.venv", text)
        self.assertIn("Python 3.10", text)
        self.assertIn("SHAKEMAP_HOST_PYTHON", text)

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


class ComposedImageValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_spatial_map_passes(self) -> None:
        path = self.root / "intensity.jpg"
        path.write_bytes(VALID_JPEG)
        result = preparation.validate_composed_image(path)
        self.assertTrue(result["passed"])
        self.assertEqual((result["width"], result["height"]), (10, 12))

    def test_known_legend_only_form_fails(self) -> None:
        path = self.root / "intensity.jpg"
        legend = bytearray(VALID_JPEG)
        frame = legend.index(b"\xff\xc0")
        legend[frame + 5:frame + 7] = (2).to_bytes(2, "big")
        legend[frame + 7:frame + 9] = (10).to_bytes(2, "big")
        path.write_bytes(legend)
        result = preparation.validate_composed_image(path)
        self.assertFalse(result["passed"])
        self.assertIn("legend/key-only", result["reason"])

    def test_unreadable_image_fails(self) -> None:
        path = self.root / "intensity.jpg"
        path.write_bytes(b"not an image")
        result = preparation.validate_composed_image(path)
        self.assertFalse(result["passed"])
        self.assertIn("unreadable", result["reason"])

    def test_missing_required_image_fails(self) -> None:
        result = preparation.validate_composed_image(self.root / "missing.jpg")
        self.assertFalse(result["passed"])
        self.assertIn("missing", result["reason"])


class ComposedPdfValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_pdf(self, width: int, height: int) -> Path:
        path = self.root / "map.pdf"
        path.write_bytes(
            b"%PDF-1.4\n1 0 obj\n<< /Type /Page /MediaBox [ 0 0 "
            + str(width).encode()
            + b" "
            + str(height).encode()
            + b" ] >>\nendobj\n%%EOF\n"
        )
        return path

    def test_spatial_page_passes(self) -> None:
        result = preparation._validate_pdf(self.write_pdf(600, 750))
        self.assertTrue(result["passed"])
        self.assertEqual(result["page_width"], 600)
        self.assertEqual(result["page_height"], 750)

    def test_legend_only_page_fails(self) -> None:
        result = preparation._validate_pdf(self.write_pdf(562, 122))
        self.assertFalse(result["passed"])
        self.assertIn("legend/key-only", result["reason"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
