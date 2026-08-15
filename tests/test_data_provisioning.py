#!/usr/bin/env python3
"""Focused tests for missing-only Stage 2 global-data provisioning."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
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

    def test_valid_existing_file_is_reused_without_download_or_mutation(self) -> None:
        target = self.root / self.spec["relative"]
        target.parent.mkdir(parents=True)
        target.write_bytes(HDF)
        before = target.stat()
        with (
            patch.object(
                preparation, "download", side_effect=AssertionError("download called")
            ),
            patch.object(
                preparation.os, "link", side_effect=AssertionError("install called")
            ),
        ):
            result = preparation.provision_file(target, self.spec, None, True)
        after = target.stat()
        self.assertEqual(result["action"], "reused")
        self.assertEqual(target.read_bytes(), HDF)
        self.assertEqual(
            (after.st_ino, after.st_mtime_ns),
            (before.st_ino, before.st_mtime_ns),
        )

    def test_manual_import_installs_only_a_missing_target(self) -> None:
        source = self.root / "manual.grd"
        source.write_bytes(HDF)
        target = self.root / self.spec["relative"]
        result = preparation.provision_file(target, self.spec, source, False)
        self.assertEqual(result["action"], "imported")
        self.assertEqual(result["path"], str(target))
        self.assertEqual(result["size"], len(HDF))
        self.assertEqual(target.read_bytes(), HDF)
        self.assertEqual(source.read_bytes(), HDF)
        self.assertEqual(list(target.parent.glob(".global_vs30.grd.install-*")), [])

    def test_download_installs_only_after_identity_validation(self) -> None:
        target = self.root / self.spec["relative"]
        destinations: list[Path] = []

        def fake_download(_url: str, destination: Path) -> None:
            destinations.append(destination)
            destination.write_bytes(HDF)

        with patch.object(preparation, "download", side_effect=fake_download):
            result = preparation.provision_file(target, self.spec, None, True)
        self.assertEqual(result["action"], "downloaded")
        self.assertEqual(destinations[0].parent, target.parent)
        self.assertEqual(target.read_bytes(), HDF)
        self.assertFalse(destinations[0].exists())

    def test_invalid_candidate_leaves_missing_target_absent(self) -> None:
        source = self.root / "bad-source.grd"
        source.write_bytes(b"invalid")
        target = self.root / self.spec["relative"]
        with self.assertRaisesRegex(
            preparation.DataProvisioningError,
            "test grid: candidate for missing asset.*identity validation.*pinned asset",
        ):
            preparation.provision_file(target, self.spec, source, False)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(".*.install-*")), [])

    def test_invalid_existing_file_fails_actionably_without_mutation(self) -> None:
        target = self.root / self.spec["relative"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"operator-invalid")
        source = self.root / "valid-source.grd"
        source.write_bytes(HDF)
        before = snapshot(self.root)
        with self.assertRaisesRegex(
            preparation.DataProvisioningError,
            rf"test grid: existing asset at {target} failed validation .*left unchanged.*Move or remove",
        ):
            preparation.provision_file(target, self.spec, source, False)
        self.assertEqual(snapshot(self.root), before)

    def test_unexpected_existing_directory_fails_without_mutation(self) -> None:
        target = self.root / self.spec["relative"]
        target.mkdir(parents=True)
        marker = target / "operator-file"
        marker.write_bytes(b"preserve")
        before = snapshot(self.root)
        with self.assertRaisesRegex(
            preparation.DataProvisioningError, "existing asset.*left unchanged"
        ):
            preparation.provision_file(target, self.spec, None, True)
        self.assertEqual(snapshot(self.root), before)

    def test_asset_appearing_during_install_is_never_replaced(self) -> None:
        source = self.root / "valid-source.grd"
        source.write_bytes(HDF)
        target = self.root / self.spec["relative"]

        def operator_publish(_candidate: Path, destination: Path) -> None:
            Path(destination).write_bytes(b"operator-race-winner")
            raise FileExistsError(destination)

        with patch.object(preparation.os, "link", side_effect=operator_publish):
            with self.assertRaisesRegex(
                preparation.DataProvisioningError,
                "asset appeared.*left unchanged.*Inspect or validate",
            ):
                preparation.provision_file(target, self.spec, source, False)
        self.assertEqual(target.read_bytes(), b"operator-race-winner")
        self.assertEqual(list(target.parent.glob(".*.install-*")), [])


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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def provision(self) -> dict:
        with patch.object(preparation, "GLOBAL_ASSETS", self.assets):
            return preparation.provision_global_data(
                self.data_root,
                vs30_source=self.vs30_source,
                topo_source=self.topo_source,
                allow_download=False,
            )

    def test_stage2_installs_only_vs30_and_topography(self) -> None:
        result = self.provision()
        self.assertEqual(set(result), {"global_assets"})
        self.assertEqual(set(result["global_assets"]), {"vs30", "topography"})
        self.assertEqual(result["global_assets"]["vs30"]["action"], "imported")
        self.assertEqual(
            (self.data_root / "global/vs30/global_vs30.grd").read_bytes(),
            self.vs30_bytes,
        )
        self.assertEqual(
            (self.data_root / "global/topo/topo_30sec.grd").read_bytes(),
            self.topo_bytes,
        )
        self.assertEqual(
            set(snapshot(self.data_root)),
            {
                "global",
                "global/topo",
                "global/topo/topo_30sec.grd",
                "global/vs30",
                "global/vs30/global_vs30.grd",
            },
        )

    def test_inspection_and_validation_are_read_only(self) -> None:
        self.provision()
        before = snapshot(self.data_root)
        with (
            patch.object(preparation, "GLOBAL_ASSETS", self.assets),
            patch.object(
                preparation,
                "sha256",
                side_effect=AssertionError("inspection hashed an asset"),
            ),
        ):
            inspection = preparation.inspect_data_assets(self.data_root)
        self.assertEqual(
            set(inspection["assets"]), {"global_vs30", "global_topography"}
        )
        with patch.object(preparation, "GLOBAL_ASSETS", self.assets):
            validation = preparation.validate_pinned_global_assets(self.data_root)
        self.assertTrue(validation["pinned_integrity_valid"])
        self.assertEqual(set(validation["global_assets"]), {"vs30", "topography"})
        self.assertEqual(snapshot(self.data_root), before)

    def test_valid_assets_are_reused_without_mutation(self) -> None:
        self.provision()
        before = snapshot(self.data_root)
        with (
            patch.object(preparation, "GLOBAL_ASSETS", self.assets),
            patch.object(
                preparation, "download", side_effect=AssertionError("download called")
            ),
        ):
            second = preparation.provision_global_data(
                self.data_root,
                vs30_source=None,
                topo_source=None,
                allow_download=True,
            )
        self.assertEqual(second["global_assets"]["vs30"]["action"], "reused")
        self.assertEqual(second["global_assets"]["topography"]["action"], "reused")
        self.assertEqual(snapshot(self.data_root), before)

    def test_invalid_existing_asset_blocks_provisioning_without_mutation(self) -> None:
        target = self.data_root / self.assets["vs30"]["relative"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"operator-invalid")
        before = snapshot(self.root)
        with patch.object(preparation, "GLOBAL_ASSETS", self.assets):
            validation = preparation.validate_pinned_global_assets(self.data_root)
            invalid = validation["global_assets"]["vs30"]
            self.assertEqual(invalid["path"], str(target))
            self.assertEqual(invalid["reason"], "size mismatch")
            self.assertIn("move or remove", invalid["corrective_action"])
            with self.assertRaisesRegex(
                preparation.DataProvisioningError,
                "global Vs30 grid.*size mismatch.*left unchanged.*Move or remove",
            ):
                preparation.provision_global_data(
                    self.data_root,
                    vs30_source=self.vs30_source,
                    topo_source=self.topo_source,
                    allow_download=False,
                )
        self.assertEqual(snapshot(self.root), before)


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
        self.assertEqual(set(choices), {"inspect", "validate", "provision"})
        provision_options = {
            option
            for action in choices["provision"]._actions
            for option in action.option_strings
        }
        self.assertEqual(
            provision_options,
            {
                "-h",
                "--help",
                "--data-root",
                "--vs30-source",
                "--topo-source",
                "--no-download",
            },
        )

    def test_data_module_and_helper_have_no_replacement_or_docker_path(self) -> None:
        project = Path(__file__).resolve().parents[1]
        module = (project / "shakemap_service/preparation.py").read_text(
            encoding="utf-8"
        )
        helper_path = project / "scripts/manage-shakemap-data.sh"
        helper = helper_path.read_text(encoding="utf-8")
        self.assertFalse((project / "scripts/configure-shakemap.sh").exists())
        for forbidden in (
            ".service/preparation",
            ".service/work",
            "container-prepare",
            "validate-record",
            "docker run",
            "subprocess.run",
            "os.replace",
        ):
            self.assertNotIn(forbidden, module)
        for forbidden in ("docker ", "--image", "replace"):
            self.assertNotIn(forbidden, helper.lower())
        self.assertNotIn("activate)", helper.lower())
        self.assertIn("inspect|validate|provision", helper)
        self.assertIn('-m shakemap_service.preparation "${ACTION}"', helper)


if __name__ == "__main__":
    unittest.main(verbosity=2)
