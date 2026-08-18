#!/usr/bin/env python3
"""Host tests for repository-owned image support manifests and installers."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shakemap_service.preparation import load_global_assets


PROJECT_DIR = Path(__file__).resolve().parent.parent
INSTALLER_PATH = PROJECT_DIR / "scripts/install-image-support.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("image_support_installer", INSTALLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load image support installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImageSupportTests(unittest.TestCase):
    def test_scientific_asset_sources_are_centralized(self) -> None:
        assets = load_global_assets()
        self.assertEqual(set(assets), {"vs30", "topography"})
        for record in assets.values():
            self.assertTrue(record["url"].startswith("https://"))
            self.assertGreater(record["size"], 0)
            self.assertRegex(record["sha256"], r"^[0-9a-f]{64}$")

        slab2 = json.loads(
            (PROJECT_DIR / "image-support/slab2.json").read_text(encoding="utf-8")
        )
        self.assertEqual(slab2["version"], "Slab2")
        self.assertEqual(slab2["file_count"], 108)
        self.assertEqual(slab2["target_subdirectory"], "slabs")
        self.assertRegex(slab2["archive"]["sha256"], r"^[0-9a-f]{64}$")

        natural_earth = json.loads(
            (PROJECT_DIR / "image-support/natural-earth-v5.1.2.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(natural_earth["tag"], "v5.1.2")
        self.assertEqual(len(natural_earth["files"]), 20)

    def test_slab_collection_is_extracted_and_inventoried(self) -> None:
        installer = load_installer()
        with tempfile.TemporaryDirectory(prefix="slab2_support_") as temporary:
            root = Path(temporary)
            source = root / "slab2.zip"
            with zipfile.ZipFile(source, "w") as bundle:
                bundle.writestr("alpha.grd", b"alpha")
                bundle.writestr("beta.grd", b"beta")
            manifest = root / "slab2.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": "Slab2",
                        "url": "https://example.test/slab2.zip",
                        "archive": {
                            "size": source.stat().st_size,
                            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        },
                        "file_count": 2,
                        "target_subdirectory": "slabs",
                    }
                ),
                encoding="utf-8",
            )
            destination = root / "installed"

            def copy_source(_url: str, target: Path) -> None:
                shutil.copyfile(source, target)

            with patch.object(installer, "_download", side_effect=copy_source):
                installer.install_slab2(manifest, destination)

            self.assertEqual((destination / "slabs/alpha.grd").read_bytes(), b"alpha")
            self.assertEqual((destination / "slabs/beta.grd").read_bytes(), b"beta")
            inventory = json.loads(
                (destination / "installed-files.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {record["path"] for record in inventory["files"]},
                {"alpha.grd", "beta.grd"},
            )
            self.assertEqual((destination / "source.zip").read_bytes(), source.read_bytes())

    def test_external_global_grids_are_not_copied_into_the_image(self) -> None:
        dockerfile = (PROJECT_DIR / "Dockerfile").read_text(encoding="utf-8")
        verifier = (PROJECT_DIR / "scripts/verify-shakemap-image.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("global_vs30.grd", dockerfile)
        self.assertNotIn("topo_30sec.grd", dockerfile)
        self.assertIn("image-support/slab2.json", dockerfile)
        self.assertIn("regional-configs /opt/shakemap-seeds/regional", dockerfile)
        self.assertIn("check command -v shake", verifier)
        self.assertIn("check sm_profile --help", verifier)
        self.assertNotIn("check shake --help", verifier)
        self.assertIn('MODE="image"', verifier)
        self.assertIn(
            "check test ! -e /home/sysop/runtime/shakemap/data/global/vs30/global_vs30.grd",
            verifier,
        )
        self.assertIn("validate_pinned_global_assets", verifier)
        self.assertIn("scenario-manifest.json", dockerfile)
        self.assertIn(
            "verification/scenarios/v4.4.9/south-napa-global/event.xml",
            dockerfile,
        )
        self.assertIn("verification/packages/v4.4.9/source-manifest.json", dockerfile)
        self.assertNotIn("COPY tests/", dockerfile)
        self.assertIn("scenario inventory must contain exactly two files", verifier)
        self.assertIn("scenario compatibility mismatch", verifier)
        self.assertIn("scenario file identity mismatch", verifier)

    def test_image_labels_and_runtime_environment_are_minimal(self) -> None:
        dockerfile = (PROJECT_DIR / "Dockerfile").read_text(encoding="utf-8")
        labels = set(
            re.findall(r"(?:^|\s)(org\.[a-z0-9_.-]+)=", dockerfile, re.MULTILINE)
        )
        self.assertEqual(
            labels,
            {
                "org.opencontainers.image.created",
                "org.usgs.shakemap.release",
                "org.usgs.shakemap.version",
                "org.usgs.shakemap.commit",
            },
        )
        self.assertNotIn("SHAKEMAP_PROFILE", dockerfile)
        self.assertNotIn("SHAKEMAP_REQUIRE_MOUNT", dockerfile)
        self.assertIn("SHAKEMAP_STREC_DB", dockerfile)
        env_block = dockerfile[dockerfile.index("ENV ") : dockerfile.index("\n\n", dockerfile.index("ENV "))]
        self.assertNotIn("DEBIAN_FRONTEND", env_block)
        self.assertIn("RUN DEBIAN_FRONTEND=noninteractive apt-get update", dockerfile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
