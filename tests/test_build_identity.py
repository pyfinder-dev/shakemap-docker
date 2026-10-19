#!/usr/bin/env python3
"""Deterministic host tests for build and deployment identity."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from shakemap_service import build_identity, main, release


COMMIT_A = "a" * 40
IMAGE_ID = "sha256:" + "1" * 64
IMAGE_DIGEST = "registry.example/shakemap@sha256:" + "2" * 64


def identity_manifest() -> dict:
    return {
        "schema_version": build_identity.BUILD_IDENTITY_SCHEMA_VERSION,
        "immutable_image": {
            "available": True,
            "upstream": {
                "repository_url": release.OFFICIAL_REPOSITORY_URL,
                "release_tag": "v4.4.10",
                "source_commit": COMMIT_A,
            },
            "installed": {
                "shakemap_distribution_version": "4.4.10",
                "shakemap_modules_distribution_version": "1.2.3",
                "python_version": "3.12.7",
                "dependency_inventory_path": "/opt/shakemap-build/dependencies.txt",
                "mapping_compatibility": {
                    "schema_version": 1,
                    "policy": "resolved_release_conda_lock",
                    "distribution": "matplotlib",
                    "platform": "linux-64",
                    "source_lock_path": "/opt/shakemap/conda-lock.yml",
                    "before_version": "3.11.1",
                    "locked_version": "3.10.8",
                    "installed_version": "3.10.8",
                    "changed": True,
                    "record_path": "/opt/shakemap-build/mapping-compatibility.json",
                },
            },
            "support": {
                "natural_earth": {
                    "tag": "v5.1.2",
                    "commit": "e" * 40,
                    "manifest_path": "/opt/shakemap-support/natural-earth-v5.1.2.json",
                    "manifest_sha256": "f" * 64,
                    "cartopy_data_dir": "/opt/shakemap-support/cartopy",
                    "file_count": 20,
                    "layers": ["cultural/admin_0_countries"],
                },
                "strec": {
                    "distribution_version": "2.3.14",
                    "database_path": "/site-packages/strec/data/moment_tensors.db",
                    "database_link": "/opt/shakemap-support/strec/moment_tensors.db",
                    "database_size": 10,
                    "database_sha256": "a" * 64,
                    "database_is_installed_distribution_file": True,
                },
                "slab2": {
                    "version": "Slab2",
                    "source_url": "https://example.test/slab2.zip",
                    "source_archive_path": "/opt/shakemap-support/slab2/source.zip",
                    "source_archive_sha256": "1" * 64,
                    "source_manifest_path": "/opt/shakemap-support/slab2.json",
                    "source_manifest_sha256": "2" * 64,
                    "installed_files_manifest_path": "/opt/shakemap-support/slab2/installed-files.json",
                    "installed_files_manifest_sha256": "3" * 64,
                    "slabs_dir": "/opt/shakemap-support/slab2/slabs",
                    "file_count": 108,
                },
            },
            "built_at_utc": "2026-07-22T12:00:00Z",
        },
    }


def make_support(root: Path) -> tuple[Path, Path, Path, Path]:
    cartopy = root / "cartopy"
    records = []
    for index in range(20):
        relative = f"shapefiles/natural_earth/test/file-{index}.bin"
        path = cartopy / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"natural-earth-{index}\n".encode()
        path.write_bytes(data)
        records.append({
            "source_path": f"source-{index}",
            "target_path": relative,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    manifest = root / "natural-earth.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "tag": "v5.1.2",
        "commit": "e" * 40,
        "layers": ["cultural/admin_0_countries"],
        "files": records,
    }), encoding="utf-8")
    database = root / "strec/data/moment_tensors.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"strec database\n")
    link = root / "strec-link.db"
    link.symlink_to(database)
    return manifest, cartopy, database, link


def make_slab2_support(root: Path) -> tuple[Path, Path]:
    support = root / "slab2-support"
    slabs = support / "slabs"
    slabs.mkdir(parents=True, exist_ok=True)
    records = []
    for index in range(2):
        path = slabs / f"grid-{index}.grd"
        data = f"slab2-{index}\n".encode()
        path.write_bytes(data)
        records.append(
            {
                "path": path.name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    archive = support / "source.zip"
    archive.write_bytes(b"slab2 archive fixture\n")
    inventory = support / "installed-files.json"
    inventory.write_text(
        json.dumps({"schema_version": 1, "files": records}),
        encoding="utf-8",
    )
    manifest = root / "slab2.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset": "USGS Slab2 grid collection",
                "version": "Slab2",
                "url": "https://example.test/slab2.zip",
                "archive": {
                    "size": archive.stat().st_size,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                },
                "file_count": len(records),
                "target_subdirectory": "slabs",
            }
        ),
        encoding="utf-8",
    )
    return manifest, support


class BuildIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="build_identity_")
        self.root = Path(self.tempdir.name)
        self.manifest_path = self.root / "identity.json"
        self.manifest_path.write_text(json.dumps(identity_manifest()), encoding="utf-8")
        self.path_patch = patch.object(build_identity, "IDENTITY_PATH", self.manifest_path)
        self.path_patch.start()
        self.env = patch.dict(
            os.environ,
            {
                "SHAKEMAP_BUILD_IDENTITY_FILE": str(self.root / "attacker.json"),
                "SHAKEMAP_IMAGE_ID": IMAGE_ID,
                "SHAKEMAP_IMAGE_DIGEST": "",
            },
        )
        self.env.start()
        build_identity.clear_identity_cache()

    def tearDown(self) -> None:
        build_identity.clear_identity_cache()
        self.env.stop()
        self.path_patch.stop()
        self.tempdir.cleanup()

    def test_manifest_path_is_fixed_and_environment_cannot_redirect_it(self) -> None:
        loaded = build_identity.load_build_identity()
        self.assertTrue(loaded["immutable_image"]["available"])
        self.assertEqual(loaded["immutable_image"]["upstream"]["source_commit"], COMMIT_A)
        self.assertEqual(build_identity.IDENTITY_PATH, self.manifest_path)

    def test_missing_fixed_manifest_reports_unavailable(self) -> None:
        missing = build_identity.load_build_identity(self.root / "missing.json")
        self.assertFalse(missing["immutable_image"]["available"])
        self.assertEqual(
            missing["immutable_image"]["manifest_path"], str(self.root / "missing.json")
        )

    def test_manifest_requires_exact_official_repository(self) -> None:
        for repository_url in (
            release.OFFICIAL_REPOSITORY_URL + "/mirror",
            release.OFFICIAL_REPOSITORY_URL.removesuffix(".git"),
        ):
            bad = identity_manifest()
            bad["immutable_image"]["upstream"]["repository_url"] = repository_url
            with self.subTest(repository_url=repository_url), self.assertRaises(
                build_identity.BuildIdentityError
            ):
                build_identity.validate_build_identity(bad)

    def test_identity_schemas_are_literal_version_1_0(self) -> None:
        self.assertEqual(build_identity.BUILD_IDENTITY_SCHEMA_VERSION, "1.0")
        self.assertEqual(build_identity.SERVICE_IDENTITY_SCHEMA_VERSION, "1.0")
        for obsolete in (2, 3, 4, "2", "3", "4"):
            invalid = identity_manifest()
            invalid["schema_version"] = obsolete
            with self.subTest(obsolete=obsolete), self.assertRaisesRegex(
                build_identity.BuildIdentityError, "schema_version"
            ):
                build_identity.validate_build_identity(invalid)

    def test_environment_compatibility_is_accepted_alongside_legacy_reader(self) -> None:
        manifest = identity_manifest()
        installed = manifest["immutable_image"]["installed"]
        del installed["mapping_compatibility"]
        installed["environment_compatibility"] = {
            "schema_version": 1,
            "policy": "resolved_release_environment",
            "source_environment_path": "/opt/shakemap/environment.yml",
            "source_environment_sha256": "a" * 64,
            "record_path": "/opt/shakemap-build/environment-compatibility.json",
            "requirements": ["numpy<2.5,>=1.26"],
            "installed_versions": {"numpy": "2.4.0"},
            "native_versions": {"proj": {"pyproj": "9.7.1"}},
            "native_requirements": {"proj": "proj<9.8"},
            "provider_differences": {"lock_replication": False},
            "native_check_scope": "exposed runtime libraries",
        }
        self.assertTrue(build_identity.validate_build_identity(manifest))
        for field, value in (("policy", "invented"), ("requirements", []), ("source_environment_sha256", "bad")):
            invalid = json.loads(json.dumps(manifest))
            invalid["immutable_image"]["installed"]["environment_compatibility"][field] = value
            with self.subTest(field=field), self.assertRaises(build_identity.BuildIdentityError):
                build_identity.validate_build_identity(invalid)

    def test_deployment_identity_accepts_only_supported_docker_formats(self) -> None:
        with patch.dict(
            os.environ,
            {"SHAKEMAP_IMAGE_ID": IMAGE_ID, "SHAKEMAP_IMAGE_DIGEST": IMAGE_DIGEST},
        ):
            deployment = build_identity.deployment_identity()
        self.assertTrue(deployment["available"])
        self.assertEqual(deployment["image_id"], IMAGE_ID)
        self.assertEqual(deployment["image_digest"], IMAGE_DIGEST)
        self.assertEqual(deployment["invalid_fields"], [])

        with patch.dict(
            os.environ,
            {"SHAKEMAP_IMAGE_ID": "sha256:not-a-digest", "SHAKEMAP_IMAGE_DIGEST": "latest"},
        ):
            invalid = build_identity.deployment_identity()
        self.assertFalse(invalid["available"])
        self.assertIsNone(invalid["image_id"])
        self.assertIsNone(invalid["image_digest"])
        self.assertEqual(invalid["invalid_fields"], ["image_id", "image_digest"])
        self.assertEqual(invalid["source"], "invalid_runtime_environment")

        with patch.dict(
            os.environ,
            {"SHAKEMAP_IMAGE_ID": "", "SHAKEMAP_IMAGE_DIGEST": ""},
        ):
            unavailable = build_identity.deployment_identity()
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["invalid_fields"], [])
        self.assertEqual(unavailable["source"], "unavailable")

    def test_config_and_health_use_shared_identity_loader(self) -> None:
        config_response = main.get_config()
        health_response = main.healthz()
        self.assertEqual(config_response["identity"]["deployment"]["image_id"], IMAGE_ID)
        self.assertEqual(config_response["shakemap_version"], "4.4.10")
        self.assertEqual(health_response["shakemap_version"], "4.4.10")
        self.assertEqual(config_response["readiness"]["ready"], False)
        self.assertEqual(health_response["ready"], False)
        self.assertEqual(
            config_response["readiness"]["reason"],
            health_response["reason"],
        )

    def test_writer_api_validates_and_writes_manifest(self) -> None:
        dependencies = self.root / "dependencies.txt"
        dependencies.write_text("example==1.0\n", encoding="utf-8")
        output = self.root / "written.json"
        natural_manifest, cartopy, strec_database, strec_link = make_support(self.root / "support-api")
        slab2_manifest, slab2_support = make_slab2_support(self.root / "support-api")
        # Exercise the new writer without importing the scientific stack on the
        # host: native version verification belongs to the built image.
        environment = {
            "schema_version": 1,
            "policy": "resolved_release_environment",
            "source_environment_path": "/opt/shakemap/environment.yml",
            "source_environment_sha256": "a" * 64,
            "requirements": ["numpy<2.5,>=1.26"],
            "installed_versions": {"numpy": "2.4.0"},
            "native_versions": {"proj": {"pyproj": "9.7.1"}},
            "native_requirements": {"proj": "proj<9.8"},
            "provider_differences": {"lock_replication": False},
            "native_check_scope": "exposed runtime libraries",
        }
        environment_path = self.root / "environment-compatibility.json"
        environment_path.write_text(json.dumps(environment))
        class FakeStrec:
            version = "2.3.14"
            files = [Path("strec/data/moment_tensors.db")]
            def locate_file(self, item):
                return strec_database
        with patch.object(
            build_identity,
            "_distribution_version",
            side_effect=lambda name: "4.4.10" if name == "shakemap" else "1.2.3",
        ), patch.object(build_identity.importlib.metadata, "distribution", return_value=FakeStrec()), patch(
            "shakemap_service.upstream_environment.verify", return_value=environment
        ) as verify_environment:
            manifest = build_identity.write_build_identity(
                output=output,
                dependencies=dependencies,
                source_url=release.OFFICIAL_REPOSITORY_URL,
                release_tag="v4.4.10",
                release_version="4.4.10",
                source_commit=COMMIT_A,
                build_timestamp_utc="2026-07-22T12:00:00Z",
                natural_earth_manifest=natural_manifest,
                cartopy_data_dir=cartopy,
                slab2_manifest=slab2_manifest,
                slab2_support_dir=slab2_support,
                environment_compatibility_record=environment_path,
                strec_database_link=strec_link,
            )
        verify_environment.assert_called_once_with(Path("/opt/shakemap"))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), manifest)
        self.assertNotIn("service", manifest["immutable_image"])
        installed = manifest["immutable_image"]["installed"]
        self.assertEqual(installed["dependency_inventory_path"], str(dependencies))
        self.assertEqual(dependencies.read_text(), "example==1.0\n")
        self.assertNotIn("dependency_inventory_sha256", installed)
        compatibility = installed["environment_compatibility"]
        self.assertEqual(compatibility["installed_versions"], {"numpy": "2.4.0"})
        self.assertEqual(compatibility["source_environment_sha256"], "a" * 64)
        self.assertEqual(compatibility["record_path"], str(environment_path))

    def test_module_cli_requires_environment_record(self) -> None:
        # The API test covers a full write. CLI validation must reject an
        # omitted compatibility record before reading installation metadata.
        output = self.root / "cli-identity.json"
        result = subprocess.run(
            [
                sys.executable, "-m", "shakemap_service.build_identity", "write",
                "--output", str(output),
                "--dependencies", str(self.root / "dependencies.txt"),
                "--source-url", release.OFFICIAL_REPOSITORY_URL,
                "--release-tag", "v4.4.10",
                "--release-version", "4.4.10",
                "--source-commit", COMMIT_A,
                "--build-timestamp-utc", "2026-07-22T12:00:00Z",
                "--natural-earth-manifest", str(self.root / "natural-earth.json"),
                "--cartopy-data-dir", str(self.root / "cartopy"),
                "--slab2-manifest", str(self.root / "slab2.json"),
                "--slab2-support-dir", str(self.root / "slab2-support"),
            ],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("required: --environment-compatibility-record", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
