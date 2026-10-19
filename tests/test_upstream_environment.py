"""Check upstream environment normalization and installed compatibility gates."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from shakemap_service import upstream_environment as upstream


ENVIRONMENT = """name: shakemap
dependencies:
  - python>=3.12,<3.13
  - python-build
  - numpy>=1.26,<2.5
  - numba=0.66.*
  - matplotlib>=3.8,<3.11
  - pyproj
  - proj<9.8
  - hdf5=2.1.*
  - libgdal-hdf5
  - libgdal-netcdf
  - libblas=3.9.*=*_newaccelerate # [osx]
  - pip
  - pip:
      - shakemap-modules[all]>=1.2.5
      - esi-shakelib>=1.2.7
"""
VERSIONS = {
    "build": "1.2.0", "numpy": "2.4.0", "numba": "0.66.1",
    "matplotlib": "3.10.8", "pyproj": "3.7.2",
    "shakemap-modules": "1.2.5", "esi-shakelib": "1.2.7",
}
NATIVE = {
    "proj": {"pyproj": "9.7.1", "rasterio": "9.7.1", "fiona": "9.7.1"},
    "hdf5": {"h5py": "2.1.0", "netCDF4": "2.1.0"},
}
CRS_CHECKS = {name: "EPSG:4326" for name in ("pyproj", "rasterio", "fiona")}


class UpstreamEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "environment.yml").write_text(ENVIRONMENT)

    def verify(self, versions=None, native=None, output=None):
        with patch.object(importlib.metadata, "version", side_effect=(versions or VERSIONS).__getitem__), \
             patch.object(upstream, "_native_versions", return_value=native or NATIVE), \
             patch.object(upstream, "_crs_checks", return_value=CRS_CHECKS), \
             patch.object(upstream.platform, "python_version", return_value="3.12.8"):
            return upstream.verify(self.root, output)

    def test_linux_normalization_preserves_all_native_and_python_requirements(self):
        output = self.root / "environment-linux.yml"
        upstream.normalize(self.root, output)
        self.assertEqual(
            output.read_text(),
            "\n".join(line for line in ENVIRONMENT.splitlines() if "# [osx]" not in line) + "\n",
        )
        self.assertIn("- hdf5=2.1.*", output.read_text())
        self.assertIn("- libgdal-hdf5", output.read_text())
        self.assertIn("- shakemap-modules[all]>=1.2.5", output.read_text())

    def test_validation_bounds_follow_source_edits_without_version_constants(self):
        record = upstream.read_environment(self.root)
        self.assertIn("numpy<2.5,>=1.26", record["requirements"])
        self.assertIn("numba==0.66.*", record["requirements"])
        self.assertIn("esi-shakelib>=1.2.7", record["requirements"])
        (self.root / "environment.yml").write_text(ENVIRONMENT.replace("<2.5", "<2.6"))
        self.assertIn("numpy<2.6,>=1.26", upstream.read_environment(self.root)["requirements"])

    def test_arm64_core_provider_changes_without_changing_upstream_specifier(self):
        import yaml

        content = ENVIRONMENT.replace("  - python-build", "  - python-build\n  - esi-core>=1.2.9")
        (self.root / "environment.yml").write_text(content)
        output = self.root / "environment-linux.yml"
        for system, architecture in (("Linux", "aarch64"), ("Linux", "x86_64"), ("Darwin", "arm64")):
            with patch.object(upstream.platform, "system", return_value=system), \
                 patch.object(upstream.platform, "machine", return_value=architecture):
                upstream.normalize(self.root, output)
                dependencies = yaml.safe_load(output.read_text())["dependencies"]
                pip = next(entry["pip"] for entry in dependencies if isinstance(entry, dict))
                record = upstream.read_environment(self.root)
            with self.subTest(system=system, architecture=architecture):
                adapted = system == "Linux" and architecture == "aarch64"
                self.assertEqual("esi-core>=1.2.9" in pip, adapted)
                self.assertEqual("esi-core>=1.2.9" in dependencies, not adapted)
                self.assertEqual(bool(record["provider_differences"]["provider_adaptations"]), adapted)
                self.assertIn("esi-core>=1.2.9", record["requirements"])

        # A future source bound must be carried through, not frozen to 4.5's value.
        (self.root / "environment.yml").write_text(content.replace("esi-core>=1.2.9", "esi-core>=1.3,<2"))
        with patch.object(upstream.platform, "system", return_value="Linux"), \
             patch.object(upstream.platform, "machine", return_value="aarch64"):
            upstream.normalize(self.root, output)
        self.assertIn("      - esi-core>=1.3,<2", output.read_text())

    def test_unsupported_selector_and_native_constraint_fail_for_review(self):
        for content in (
            ENVIRONMENT.replace("[osx]", "[win]"),
            ENVIRONMENT.replace("- libgdal-hdf5", "- libgdal-hdf5>=4"),
            ENVIRONMENT.replace("- numba=0.66.*", "- numba=0.66.*=a_build"),
        ):
            (self.root / "environment.yml").write_text(content)
            with self.subTest(content=content), self.assertRaises(upstream.UpstreamEnvironmentError):
                upstream.read_environment(self.root)

    def test_installed_record_contains_actual_versions_and_source_provenance(self):
        output = self.root / "record.json"
        record = self.verify(output=output)
        self.assertEqual(record["installed_versions"], VERSIONS)
        self.assertEqual(record["native_versions"], NATIVE)
        self.assertEqual(record["installation_checks"]["crs_database"], CRS_CHECKS)
        self.assertEqual(record, json.loads(output.read_text()))
        self.assertFalse(record["provider_differences"]["lock_replication"])
        self.assertEqual(len(record["source_environment_sha256"]), 64)

    def test_crs_checks_query_each_provider_and_reject_failure(self):
        from unittest.mock import Mock

        classes = {name: Mock() for name in CRS_CHECKS}
        modules = {
            "pyproj": types.ModuleType("pyproj"),
            "fiona": types.ModuleType("fiona"),
            "fiona.crs": types.ModuleType("fiona.crs"),
            "rasterio": types.ModuleType("rasterio"),
            "rasterio.crs": types.ModuleType("rasterio.crs"),
        }
        modules["pyproj"].CRS = classes["pyproj"]
        for name in ("fiona", "rasterio"):
            modules[name].crs = modules[f"{name}.crs"]
            modules[f"{name}.crs"].CRS = classes[name]
        for cls in classes.values():
            cls.from_epsg.return_value.to_epsg.return_value = 4326
        with patch.dict("sys.modules", modules):
            self.assertEqual(upstream._crs_checks(), CRS_CHECKS)
            for name, cls in classes.items():
                cls.from_epsg.assert_called_once_with(4326)
                cls.from_epsg.side_effect = RuntimeError("proj.db unavailable")
                with self.subTest(provider=name), self.assertRaisesRegex(upstream.UpstreamEnvironmentError, name):
                    upstream._crs_checks()
                cls.from_epsg.side_effect = None

    def test_numpy_boundary_and_each_native_provider_are_enforced(self):
        versions = dict(VERSIONS, numpy="2.5.3")
        with self.assertRaisesRegex(upstream.UpstreamEnvironmentError, "numpy.*2.5.3"):
            self.verify(versions=versions)
        for library, provider, version in (
            ("proj", "pyproj", "9.8.1"),
            ("proj", "rasterio", "9.8.1"),
            ("proj", "fiona", "9.8.1"),
            ("hdf5", "h5py", "2.0.0"),
            ("hdf5", "netCDF4", "1.14.6"),
        ):
            native = json.loads(json.dumps(NATIVE))
            native[library][provider] = version
            with self.subTest(provider=provider), self.assertRaisesRegex(
                upstream.UpstreamEnvironmentError, provider
            ):
                self.verify(native=native)

    def test_record_check_is_read_only_and_rejects_changed_inventory(self):
        path = self.root / "record.json"
        record = self.verify(output=path)
        before = path.stat().st_mtime_ns
        with patch.object(upstream, "verify", return_value=record):
            self.assertEqual(upstream.main(["check", "--source", str(self.root), "--record", str(path)]), 0)
        self.assertEqual(before, path.stat().st_mtime_ns)
        record["installed_versions"]["numpy"] = "2.3.0"
        with patch.object(upstream, "verify", return_value=record), self.assertRaises(SystemExit) as failure:
            upstream.main(["check", "--source", str(self.root), "--record", str(path)])
        self.assertEqual(failure.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
