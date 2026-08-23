"""Host-side tests for verification package definition and preparation safety."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
HELPER_PATH = PROJECT / "scripts" / "prepare-shakemap-verification-data.py"
SPEC = importlib.util.spec_from_file_location("verification_data_helper", HELPER_PATH)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tiny_definition(raw: bytes, archive: bytes, member: bytes) -> dict:
    return {
        "schema_version": 2,
        "package_id": "tiny-verification-package",
        "description": "test definition",
        "compatibility": {
            "shakemap_release_tag": "v0.0.0",
            "shakemap_version": "0.0.0",
            "shakemap_source_commit": "0" * 40,
            "shakemap_modules_version": "0.0.0",
            "usgs_strec_version": "0.0.0",
        },
        "module_plan": helper.MODULE_PLAN,
        "default_destination": "runtime/shakemap/data/test/v0.0.0",
        "coverage": {
            "event_latitude": 0.0,
            "event_longitude": 0.0,
            "statement": "test only",
        },
        "licenses": {"test": {"summary": "test", "url": "https://example.test"}},
        "image_dependencies": {"test": "generic support supplied elsewhere"},
        "sources": [
            {
                "id": "raw",
                "kind": "raw-files",
                "url_prefix": "https://example.test/commit/",
                "license_id": "test",
                "files": [
                    {
                        "source_path": "source/input.bin",
                        "target_path": "config/input.bin",
                        "size": len(raw),
                        "sha256": digest(raw),
                        "transformation": "copied unchanged",
                    }
                ],
            },
            {
                "id": "archive",
                "kind": "zip-member",
                "url": "https://example.test/archive.whl",
                "source_filename": "archive.whl",
                "source_size": len(archive),
                "source_sha256": digest(archive),
                "license_id": "test",
                "files": [
                    {
                        "source_path": "data/member.db",
                        "target_path": "data/strec/member.db",
                        "size": len(member),
                        "sha256": digest(member),
                        "transformation": "extracted unchanged",
                    }
                ],
            },
        ],
        "generated_files": [],
        "limitations": ["test package only", "Uniform VS30 is not used."],
    }


class PreparationBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "sources"
        raw = b"release configuration\n"
        member = b"moment database\n"
        raw_path = self.source_dir / "raw" / "source" / "input.bin"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_bytes(raw)
        archive_path = self.source_dir / "archive" / "archive.whl"
        archive_path.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("data/member.db", member)
        self.definition = tiny_definition(raw, archive_path.read_bytes(), member)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manual_import_and_validation(self) -> None:
        destination = self.root / "prepared"
        state, manifest = helper.prepare_package(
            self.definition, destination, self.source_dir
        )
        self.assertEqual(state, "prepared")
        self.assertEqual(manifest["package_id"], "tiny-verification-package")
        self.assertEqual(
            manifest["compressed_archive_bytes"],
            len((self.source_dir / "archive" / "archive.whl").read_bytes()),
        )
        self.assertTrue((destination / "README.md").is_file())
        self.assertTrue((destination / "package-manifest.json").is_file())
        self.assertEqual(
            (destination / "config" / "input.bin").read_bytes(),
            b"release configuration\n",
        )
        helper.validate_package(self.definition, destination)
        readme = (destination / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Exact sources", readme)
        self.assertIn("https://example.test/commit/", readme)
        self.assertIn("https://example.test/archive.whl", readme)
        self.assertIn("\n## Validate\n", readme)
        removed_command = "-".join(("run", "native"))
        removed_manifest_field = "_".join(
            ("native", "validation", "command")
        )
        self.assertIn("validation_command", manifest)
        self.assertNotIn(removed_command, readme)
        self.assertNotIn(removed_manifest_field, manifest)

    def test_schema_two_manifest_accepts_historical_extra_field(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        manifest_path = destination / "package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        historical_field = "_".join(("native", "validation", "command"))
        manifest[historical_field] = "retained historical metadata"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        validated = helper.validate_package(self.definition, destination)

        self.assertEqual(
            validated[historical_field], "retained historical metadata"
        )

    def test_checksum_verified_download_mode(self) -> None:
        destination = self.root / "downloaded"
        definition = json.loads(json.dumps(self.definition))
        definition["sources"][0]["url_prefix"] = (
            self.source_dir.joinpath("raw").as_uri() + "/"
        )
        definition["sources"][1]["url"] = (
            self.source_dir / "archive" / "archive.whl"
        ).as_uri()
        state, _ = helper.prepare_package(definition, destination)
        self.assertEqual(state, "prepared")
        helper.validate_package(definition, destination)

    def test_valid_destination_is_not_overwritten(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        manifest_path = destination / "package-manifest.json"
        before = manifest_path.read_bytes()
        state, _ = helper.prepare_package(self.definition, destination, self.source_dir)
        self.assertEqual(state, "already-valid")
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_regular_ds_store_files_are_ignored_and_preserved(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        metadata = {
            destination / ".DS_Store": b"root metadata",
            destination / "config" / ".DS_Store": b"nested metadata",
        }
        for path, content in metadata.items():
            path.write_bytes(content)

        manifest = helper.validate_package(self.definition, destination)

        self.assertFalse(
            any(".DS_Store" in entry["installed_path"] for entry in manifest["files"])
        )
        for path, content in metadata.items():
            self.assertEqual(path.read_bytes(), content)

    def test_existing_destination_with_ds_store_files_is_reused(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        root_metadata = destination / ".DS_Store"
        nested_metadata = destination / "data" / ".DS_Store"
        root_metadata.write_bytes(b"root metadata")
        nested_metadata.write_bytes(b"nested metadata")
        manifest_path = destination / "package-manifest.json"
        manifest_before = manifest_path.read_bytes()

        state, _ = helper.prepare_package(
            self.definition, destination, self.source_dir
        )

        self.assertEqual(state, "already-valid")
        self.assertEqual(root_metadata.read_bytes(), b"root metadata")
        self.assertEqual(nested_metadata.read_bytes(), b"nested metadata")
        self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_other_unexpected_dotfile_is_rejected(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        (destination / ".hidden").write_bytes(b"unexpected")

        with self.assertRaisesRegex(helper.IntegrityError, r"unexpected=\['\.hidden'\]"):
            helper.validate_package(self.definition, destination)

    def test_unsafe_ds_store_entry_types_are_rejected(self) -> None:
        for entry_type in ("symlink", "directory", "fifo"):
            with self.subTest(entry_type=entry_type):
                destination = self.root / f"prepared-{entry_type}"
                helper.prepare_package(self.definition, destination, self.source_dir)
                metadata = destination / ".DS_Store"
                if entry_type == "symlink":
                    metadata.symlink_to(destination / "README.md")
                    expected = "symbolic link"
                elif entry_type == "directory":
                    metadata.mkdir()
                    expected = "not a regular file"
                else:
                    os.mkfifo(metadata)
                    expected = "special entry"

                with self.assertRaisesRegex(helper.IntegrityError, expected):
                    helper.validate_package(self.definition, destination)

    def test_ds_store_below_symlinked_directory_is_rejected(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / ".DS_Store").write_bytes(b"metadata")
        (destination / "linked").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(helper.IntegrityError, "symbolic link"):
            helper.validate_package(self.definition, destination)

    def test_corrupt_installed_file_is_reported(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        (destination / "config" / "input.bin").write_bytes(b"corrupt")
        with self.assertRaisesRegex(helper.IntegrityError, "corrupt installed file"):
            helper.validate_package(self.definition, destination)

    def test_incomplete_prepared_readme_is_reported(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        (destination / "README.md").write_text("incomplete\n", encoding="utf-8")
        with self.assertRaisesRegex(helper.IntegrityError, "README is incomplete"):
            helper.validate_package(self.definition, destination)

    def test_incompatible_provenance_is_reported(self) -> None:
        destination = self.root / "prepared"
        helper.prepare_package(self.definition, destination, self.source_dir)
        manifest_path = destination / "package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["source_url"] = "https://example.test/wrong-source"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(helper.IntegrityError, "wrong source_url"):
            helper.validate_package(self.definition, destination)

    def test_partial_destination_is_preserved(self) -> None:
        destination = self.root / "prepared"
        destination.mkdir()
        marker = destination / "operator-file.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(helper.DestinationError, "was not modified"):
            helper.prepare_package(self.definition, destination, self.source_dir)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_missing_and_corrupt_manual_sources_are_reported_together(self) -> None:
        (self.source_dir / "raw" / "source" / "input.bin").write_bytes(b"bad")
        (self.source_dir / "archive" / "archive.whl").unlink()
        with self.assertRaisesRegex(
            helper.MissingSourceError, "missing, corrupt, or partial"
        ) as context:
            helper.validate_manual_sources(self.definition, self.source_dir)
        self.assertIn("corrupt manual source", str(context.exception))
        self.assertIn("missing manual source", str(context.exception))


class TrackedDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = (
            PROJECT
            / "verification"
            / "packages"
            / "v4.4.9"
            / "source-manifest.json"
        )
        cls.definition = helper.load_definition(cls.path)

    def test_release_identity_and_module_plan(self) -> None:
        compatibility = self.definition["compatibility"]
        self.assertEqual(compatibility["shakemap_release_tag"], "v4.4.9")
        self.assertEqual(
            compatibility["shakemap_source_commit"],
            "8923f1ff6e82fc866d928a33d1e19e45f276db52",
        )
        self.assertEqual(self.definition["module_plan"], helper.MODULE_PLAN)

    def test_source_inventory_is_immutable_and_complete(self) -> None:
        inventory = helper.source_inventory(self.definition)
        self.assertEqual(len(inventory), 11)
        for entry in inventory:
            self.assertTrue(entry["url"].startswith("https://"))
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertGreater(entry["size"], 0)
        urls = "\n".join(entry["url"] for entry in inventory)
        self.assertIn("8923f1ff6e82fc866d928a33d1e19e45f276db52", urls)
        self.assertNotIn("f1890d9f152c896d250a77557a5751a93d494776", urls)
        self.assertNotIn("usgs_strec-2.3.14", urls)

    def test_payload_is_scenario_specific_and_excludes_image_support(self) -> None:
        payload = helper.expected_payload(self.definition)
        self.assertIn("data/vs30/CA_vs30.grd", payload)
        self.assertIn("data/mapping/CA_topo.grd", payload)
        self.assertNotIn("data/strec/moment_tensors.db", payload)
        self.assertNotIn("data/strec/config.ini.template", payload)
        cartopy = [path for path in payload if path.startswith("data/cartopy/")]
        self.assertEqual(cartopy, [])
        self.assertEqual(set(self.definition["image_dependencies"]), {"natural_earth", "strec"})
        self.assertNotIn("uniform", " ".join(payload).lower())

    def test_definition_records_licenses_coverage_and_non_claims(self) -> None:
        self.assertEqual(
            set(self.definition["licenses"]),
            {"shakemap"},
        )
        self.assertIn("vs30_bounds", self.definition["coverage"])
        limitations = " ".join(self.definition["limitations"]).lower()
        for phrase in [
            "not a global dataset",
            "authoritative success",
            "production readiness",
            "uniform vs30 is not used",
        ]:
            self.assertIn(phrase, limitations)


class CommandSurfaceTests(unittest.TestCase):
    def test_parser_exposes_package_operations_only(self) -> None:
        help_text = helper.build_parser().format_help()
        for command in ("list-sources", "prepare", "validate"):
            self.assertIn(command, help_text)

        removed_command = "-".join(("run", "native"))
        self.assertNotIn(removed_command, help_text)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                helper.build_parser().parse_args([removed_command])
        self.assertEqual(context.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
