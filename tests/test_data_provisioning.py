#!/usr/bin/env python3
"""Focused tests for explicit, contracted-path data provisioning."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shakemap_service import preparation


HDF = b"\x89HDF\r\n\x1a\n" + b"scientific-grid"


def pinned_spec(data: bytes, relative: str, label: str) -> dict:
    return {
        "label": label,
        "relative": relative,
        "url": f"https://example.test/{Path(relative).name}",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "checksum_authority": "test source checksum",
    }


def snapshot(root: Path) -> dict[str, bytes | None]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes() if path.is_file() else None
        )
        for path in sorted(root.rglob("*"))
    }


class FileProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.spec = pinned_spec(HDF, "global/vs30/global_vs30.grd", "test grid")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_existing_file_is_reused_without_download(self) -> None:
        target = self.root / self.spec["relative"]
        target.parent.mkdir(parents=True)
        target.write_bytes(HDF)
        with patch.object(
            preparation, "download", side_effect=AssertionError("download called")
        ):
            result = preparation.provision_file(target, self.spec, None, True)
        self.assertEqual(result["action"], "reused")
        self.assertEqual(target.read_bytes(), HDF)

    def test_manual_import_is_an_atomic_sibling_install(self) -> None:
        source = self.root / "manual.grd"
        source.write_bytes(HDF)
        target = self.root / self.spec["relative"]
        result = preparation.provision_file(target, self.spec, source, False)
        self.assertEqual(result["action"], "imported")
        self.assertEqual(target.read_bytes(), HDF)
        self.assertEqual(list(target.parent.glob(".global_vs30.grd.install-*")), [])

    def test_mocked_download_is_an_atomic_sibling_install(self) -> None:
        target = self.root / self.spec["relative"]
        destinations = []

        def fake_download(_url: str, destination: Path) -> None:
            destinations.append(destination)
            destination.write_bytes(HDF)

        with patch.object(preparation, "download", side_effect=fake_download):
            result = preparation.provision_file(target, self.spec, None, True)
        self.assertEqual(result["action"], "downloaded")
        self.assertEqual(destinations[0].parent, target.parent)
        self.assertEqual(target.read_bytes(), HDF)
        self.assertFalse(destinations[0].exists())

    def test_invalid_replacement_cannot_corrupt_existing_target(self) -> None:
        target = self.root / self.spec["relative"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"operator-invalid-but-preserved")
        source = self.root / "bad-source.grd"
        source.write_bytes(b"also-invalid")
        before = target.read_bytes()
        with self.assertRaisesRegex(
            preparation.DataProvisioningError, "replacement.*invalid"
        ):
            preparation.provision_file(target, self.spec, source, False)
        self.assertEqual(target.read_bytes(), before)

    def test_valid_replacement_preserves_invalid_operator_file(self) -> None:
        target = self.root / self.spec["relative"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"operator-invalid")
        source = self.root / "valid-source.grd"
        source.write_bytes(HDF)
        result = preparation.provision_file(target, self.spec, source, False)
        self.assertEqual(target.read_bytes(), HDF)
        self.assertEqual(
            Path(result["preserved_invalid_path"]).read_bytes(),
            b"operator-invalid",
        )


class ContractedDestinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.vs30_bytes = HDF + b"-vs30"
        self.topo_bytes = HDF + b"-topo"
        self.assets = {
            "vs30": pinned_spec(
                self.vs30_bytes,
                "global/vs30/global_vs30.grd",
                "global Vs30 grid",
            ),
            "topography": pinned_spec(
                self.topo_bytes,
                "global/topo/topo_30sec.grd",
                "global topography grid",
            ),
        }
        self.vs30_source = self.root / "vs30.grd"
        self.topo_source = self.root / "topo.grd"
        self.vs30_source.write_bytes(self.vs30_bytes)
        self.topo_source.write_bytes(self.topo_bytes)
        self.slab_source = self.root / "slab2.zip"
        with zipfile.ZipFile(self.slab_source, "w") as bundle:
            for suffix in ("dep", "dip", "str", "unc"):
                bundle.writestr(
                    f"tst_slab2_{suffix}_01.01.01.grd",
                    HDF + suffix.encode(),
                )
        self.slab_spec = {
            "label": "Slab2 archive",
            "url": "https://example.test/slab2.zip",
            "size": self.slab_source.stat().st_size,
            "sha256": preparation.sha256(self.slab_source),
            "checksum_authority": "test",
            "file_count": 4,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def provision(self) -> dict:
        with (
            patch.object(preparation, "GLOBAL_ASSETS", self.assets),
            patch.object(preparation, "SLAB2", self.slab_spec),
        ):
            return preparation.provision_global_data(
                self.data_root,
                vs30_source=self.vs30_source,
                topo_source=self.topo_source,
                slab_source=self.slab_source,
                allow_download=False,
            )

    def test_global_assets_and_slabs_use_only_contracted_paths(self) -> None:
        result = self.provision()
        self.assertEqual(result["global_assets"]["vs30"]["action"], "imported")
        self.assertEqual(
            (self.data_root / "global/vs30/global_vs30.grd").read_bytes(),
            self.vs30_bytes,
        )
        self.assertEqual(
            (self.data_root / "global/topo/topo_30sec.grd").read_bytes(),
            self.topo_bytes,
        )
        self.assertTrue((self.data_root / "global/strec/slabs").is_dir())
        self.assertTrue(
            (self.data_root / "global/strec/slab2-manifest.json").is_file()
        )
        all_paths = set(snapshot(self.root))
        for forbidden in (
            "incoming",
            ".service/work",
            ".service/preparation",
            "events",
            "work",
            "archive",
            "preparation",
        ):
            self.assertFalse(
                any(part == forbidden for path in all_paths for part in path.split("/"))
            )

    def test_validation_is_read_only_and_reuses_valid_assets(self) -> None:
        self.provision()
        before = snapshot(self.data_root)
        with (
            patch.object(preparation, "GLOBAL_ASSETS", self.assets),
            patch.object(preparation, "SLAB2", self.slab_spec),
        ):
            validation = preparation.validate_pinned_global_assets(self.data_root)
            second = preparation.provision_global_data(
                self.data_root,
                vs30_source=None,
                topo_source=None,
                slab_source=None,
                allow_download=False,
            )
        self.assertEqual(
            validation["validation_scope"], "pinned_content_integrity"
        )
        self.assertTrue(validation["pinned_integrity_valid"])
        self.assertEqual(second["global_assets"]["vs30"]["action"], "reused")
        self.assertEqual(second["global_assets"]["topography"]["action"], "reused")
        self.assertEqual(second["slabs"]["action"], "reused")
        self.assertEqual(snapshot(self.data_root), before)

    def test_invalid_slab_archive_does_not_change_existing_tree(self) -> None:
        self.provision()
        before = snapshot(self.data_root)
        bad_archive = self.root / "bad-slab2.zip"
        with zipfile.ZipFile(bad_archive, "w") as bundle:
            bundle.writestr("../unsafe.grd", HDF)
        bad_spec = {
            **self.slab_spec,
            "size": bad_archive.stat().st_size,
            "sha256": preparation.sha256(bad_archive),
            "file_count": 1,
        }
        with patch.object(preparation, "SLAB2", bad_spec):
            with self.assertRaises(preparation.DataProvisioningError):
                preparation.provision_slabs(
                    self.data_root, bad_archive, allow_download=False
                )
        self.assertEqual(snapshot(self.data_root), before)


class SurfaceTests(unittest.TestCase):
    def test_removed_lifecycle_symbols_and_commands_are_absent(self) -> None:
        for name in (
            "docker_prepare",
            "create_profile",
            "prepare_base",
            "write_preparation_report",
            "container_prepare",
            "load_preparation",
            "run_scenario",
        ):
            self.assertFalse(hasattr(preparation, name), name)
        choices = preparation.parser()._subparsers._group_actions[0].choices
        self.assertEqual(
            set(choices),
            {
                "inspect",
                "validate-pinned-global",
                "provision-global",
            },
        )
        self.assertNotIn("container-prepare", choices)
        self.assertNotIn("validate-record", choices)

    def test_supported_source_has_no_persistent_workspace_or_docker_call(self) -> None:
        project = Path(__file__).resolve().parents[1]
        module = (project / "shakemap_service/preparation.py").read_text(
            encoding="utf-8"
        )
        script = (project / "scripts/configure-shakemap.sh").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            ".service/preparation",
            ".service/work",
            "container-prepare",
            "validate-record",
            "docker run",
            "subprocess.run",
        ):
            self.assertNotIn(forbidden, module)
        self.assertIn("provision-global", script)
        self.assertNotIn("docker ", script)
        self.assertNotIn("--image", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
