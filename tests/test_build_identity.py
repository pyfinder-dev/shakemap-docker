#!/usr/bin/env python3
"""Deterministic host tests for build and deployment identity."""
from __future__ import annotations

import json
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from shakemap_service import build_identity, main, release, runner


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
                "dependency_inventory_sha256": "d" * 64,
                "mapping_compatibility": {
                    "schema_version": 1,
                    "policy": "resolved_release_conda_lock",
                    "distribution": "matplotlib",
                    "platform": "linux-64",
                    "source_lock_path": "/opt/shakemap/conda-lock.yml",
                    "source_lock_sha256": "b" * 64,
                    "before_version": "3.11.1",
                    "locked_version": "3.10.8",
                    "installed_version": "3.10.8",
                    "changed": True,
                    "record_path": "/opt/shakemap-build/mapping-compatibility.json",
                    "record_sha256": "c" * 64,
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


def make_mapping_compatibility(root: Path, version: str) -> Path:
    path = root / "mapping-compatibility.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "policy": "resolved_release_conda_lock",
        "distribution": "matplotlib",
        "platform": "linux-64",
        "source_lock_path": "/opt/shakemap/conda-lock.yml",
        "source_lock_sha256": "b" * 64,
        "before_version": version,
        "locked_version": version,
        "installed_version": version,
        "changed": False,
    }), encoding="utf-8")
    return path


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

    def test_mapping_compatibility_comes_from_resolved_release_lock(self) -> None:
        source = self.root / "resolved-release"
        source.mkdir()
        (source / "conda-lock.yml").write_text(
            "package:\n"
            "  - name: matplotlib\n"
            "    version: 3.10.8\n"
            "    manager: pip\n"
            "    platform: linux-64\n",
            encoding="utf-8",
        )
        output = self.root / "compatibility.json"
        with patch.object(
            build_identity,
            "_distribution_version",
            side_effect=["3.11.1", "3.10.8"],
        ), patch.object(build_identity.subprocess, "run") as run:
            record = build_identity.apply_upstream_mapping_compatibility(source, output)
        self.assertEqual(record["before_version"], "3.11.1")
        self.assertEqual(record["locked_version"], "3.10.8")
        self.assertEqual(record["installed_version"], "3.10.8")
        self.assertTrue(record["changed"])
        self.assertIn("matplotlib==3.10.8", run.call_args_list[0].args[0])

    def test_mapping_compatibility_fails_closed_without_linux_lock(self) -> None:
        source = self.root / "future-release"
        source.mkdir()
        (source / "conda-lock.yml").write_text("package: []\n", encoding="utf-8")
        with self.assertRaisesRegex(build_identity.BuildIdentityError, "exactly one"):
            build_identity.apply_upstream_mapping_compatibility(
                source, self.root / "unused.json"
            )

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

    def test_config_health_and_runner_use_shared_identity_loader(self) -> None:
        config_response = main.get_config()
        health_response = main.healthz()
        self.assertEqual(config_response["identity"], health_response["identity"])
        self.assertEqual(config_response["identity"]["deployment"]["image_id"], IMAGE_ID)
        self.assertEqual(config_response["response_schema_version"], "1.0")
        self.assertEqual(health_response["response_schema_version"], "1.0")
        self.assertFalse(config_response["managed_execution_readiness"]["ready"])
        self.assertFalse(health_response["managed_execution_readiness"]["ready"])
        self.assertEqual(
            health_response["data"]["summary"]["validation_state"],
            "not_evaluated",
        )

        self.assertEqual(runner.service_identity(), main.get_config()["identity"])

    def test_writer_api_validates_and_writes_manifest(self) -> None:
        dependencies = self.root / "dependencies.txt"
        dependencies.write_text("example==1.0\n", encoding="utf-8")
        output = self.root / "written.json"
        natural_manifest, cartopy, strec_database, strec_link = make_support(self.root / "support-api")
        slab2_manifest, slab2_support = make_slab2_support(self.root / "support-api")
        mapping_compatibility = make_mapping_compatibility(self.root / "support-api", "1.2.3")
        class FakeStrec:
            version = "2.3.14"
            files = [Path("strec/data/moment_tensors.db")]
            def locate_file(self, item):
                return strec_database
        with patch.object(
            build_identity,
            "_distribution_version",
            side_effect=lambda name: "4.4.10" if name == "shakemap" else "1.2.3",
        ), patch.object(build_identity.importlib.metadata, "distribution", return_value=FakeStrec()):
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
                mapping_compatibility_record=mapping_compatibility,
                strec_database_link=strec_link,
            )
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), manifest)
        self.assertNotIn("service", manifest["immutable_image"])

    def test_module_cli_writes_manifest(self) -> None:
        metadata_root = self.root / "metadata"
        metadata_root.mkdir()
        for directory, name, version in (
            ("shakemap-4.4.10.dist-info", "shakemap", "4.4.10"),
            ("shakemap_modules-1.2.3.dist-info", "shakemap-modules", "1.2.3"),
            ("usgs_strec-2.3.14.dist-info", "usgs-strec", "2.3.14"),
            ("matplotlib-3.10.8.dist-info", "matplotlib", "3.10.8"),
        ):
            dist_info = metadata_root / directory
            dist_info.mkdir()
            (dist_info / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
                encoding="utf-8",
            )
        natural_manifest, cartopy, strec_database, strec_link = make_support(metadata_root)
        slab2_manifest, slab2_support = make_slab2_support(metadata_root)
        strec_record = metadata_root / "usgs_strec-2.3.14.dist-info" / "RECORD"
        strec_record.write_text("strec/data/moment_tensors.db,,\n", encoding="utf-8")
        dependencies = self.root / "cli-dependencies.txt"
        dependencies.write_text("example==1.0\n", encoding="utf-8")
        output = self.root / "cli-identity.json"
        mapping_compatibility = make_mapping_compatibility(self.root / "metadata", "3.10.8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join((str(metadata_root), str(PROJECT_DIR)))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "shakemap_service.build_identity",
                "write",
                "--output", str(output),
                "--dependencies", str(dependencies),
                "--source-url", release.OFFICIAL_REPOSITORY_URL,
                "--release-tag", "v4.4.10",
                "--release-version", "4.4.10",
                "--source-commit", COMMIT_A,
                "--build-timestamp-utc", "2026-07-22T12:00:00Z",
                "--natural-earth-manifest", str(natural_manifest),
                "--cartopy-data-dir", str(cartopy),
                "--slab2-manifest", str(slab2_manifest),
                "--slab2-support-dir", str(slab2_support),
                "--mapping-compatibility-record", str(mapping_compatibility),
                "--strec-database-link", str(strec_link),
            ],
            cwd=PROJECT_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(build_identity.validate_build_identity(json.loads(output.read_text())))


class StartupHelperTests(unittest.TestCase):
    def test_reserved_identity_environment_is_rejected_before_docker(self) -> None:
        script = PROJECT_DIR / "scripts" / "start-shakemap-docker.sh"
        for key in (
            "SHAKEMAP_IMAGE_ID",
            "SHAKEMAP_IMAGE_DIGEST",
            "SHAKEMAP_BUILD_IDENTITY_FILE",
        ):
            for value in (key, f"{key}=attacker", f"{key} attacker", f"{key}:attacker"):
                with self.subTest(value=value):
                    result = subprocess.run(
                        ["bash", str(script), "--env", value],
                        cwd=PROJECT_DIR,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"reserved identity key {key}", result.stderr)
                    self.assertNotIn("Checking Docker", result.stdout)

    def test_non_reserved_environment_reaches_docker_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="startup_helper_") as temp:
            root = Path(temp)
            fakebin = root / "bin"
            fakebin.mkdir()
            trace = root / "docker-run.txt"
            fake_docker = fakebin / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1 $2\" == \"image inspect\" ]]; then\n"
                "  if [[ \"$*\" == *\"{{.Id}}\"* ]]; then echo 'sha256:" + "1" * 64 + "';\n"
                "  else echo 'registry.example/shakemap@sha256:" + "2" * 64 + "'; fi\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$1 $2\" == \"container inspect\" ]]; then exit 1; fi\n"
                "if [[ \"$1\" == \"run\" ]]; then printf '%s\\n' \"$@\" > \"$TRACE_PATH\"; echo fake-id; exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join((str(fakebin), environment["PATH"]))
            environment["TRACE_PATH"] = str(trace)
            result = subprocess.run(
                [
                    "bash", str(PROJECT_DIR / "scripts" / "start-shakemap-docker.sh"),
                    "--name", "shakemap-docker-identity-qa",
                    "--runtime", str(root / "runtime"),
                    "--image", "shakemap-docker:test",
                    "--env", "IDENTITY_CHECK_MARKER=present",
                ],
                cwd=PROJECT_DIR,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = trace.read_text(encoding="utf-8").splitlines()
            self.assertIn(f"SHAKEMAP_IMAGE_ID={IMAGE_ID}", arguments)
            self.assertIn(f"SHAKEMAP_IMAGE_DIGEST={IMAGE_DIGEST}", arguments)
            self.assertIn("IDENTITY_CHECK_MARKER=present", arguments)
            combined = result.stdout + result.stderr
            self.assertNotIn(IMAGE_ID, combined)
            self.assertNotIn(IMAGE_DIGEST, combined)
            self.assertNotIn("docker run", combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
