"""Confined checks for operator diagnostics, without native or runtime work."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from shakemap_service import cli, main, paths, profile_checks
from shakemap_service.config import Settings


class ProfileCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.native = self.root / "installed-native-data"
        self.native.mkdir()
        self.regional = self.root / "shakemap/data/regional/italy"
        self.regional.mkdir(parents=True)
        self.grid = self.root / "grid.grd"
        self.grid.write_bytes(b"fixture only: no scientific validation")
        (self.native / "layers").mkdir()
        (self.native / "layers/example.wkt").write_text("fixture polygon")
        self.documents = {
            "model.conf": f"[data]\nvs30file = {self.grid}\n[modeling]\ngmpe = chosen\ngmice = intensity\nipe = intensity_prediction\nccf = correlation\n",
            "products.conf": f"[products]\n[[mapping]]\n[[[layers]]]\ntopography = {self.grid}\n",
            "select.conf": "[layers]\nlayer_dir = <INSTALL_DIR>/data/layers\n[[example]]\n[[[acr]]]\ngmpe = chosen\ngmice = intensity\n",
            "gmpe_sets.conf": "[gmpe_sets]\n[[chosen]]\ngmpes = ground_motion\nsite_gmpes = None\n",
            "modules.conf": "[gmpe_modules]\nground_motion = GroundMotion, fixture.gmpe\n[gmice_modules]\nintensity = Intensity, fixture.gmice\n[ipe_modules]\nintensity_prediction = Prediction, fixture.ipe\n[ccf_modules]\ncorrelation = Correlation, fixture.ccf\n",
        }
        for filename, content in self.documents.items():
            (self.native / filename).write_text(content)
            (self.regional / filename).write_text(content)

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            mock.patch.object(paths, "settings", Settings(runtime_root=str(self.root)))
        )
        stack.enter_context(
            mock.patch.object(
                profile_checks, "_native_data_directory", return_value=self.native
            )
        )
        stack.enter_context(
            mock.patch.object(profile_checks, "_module_available", return_value=True)
        )

    def check(self):
        return profile_checks.check_configuration("italy")

    def test_complete_static_check_never_claims_native_success(self):
        report = self.check()
        self.assertEqual(report["status"], "NO_KNOWN_BLOCKERS")
        self.assertEqual(report["scope"], "static")
        self.assertEqual(report["native_execution"], "not_run")
        self.assertNotIn("ready", report)
        self.assertTrue(report["capabilities"]["native_configuration_diagnostics"])

    def test_missing_reference_names_profile_file_key_and_exact_path(self):
        self.grid.unlink()
        report = self.check()
        finding = next(item for item in report["findings"] if item["key"] == "vs30file")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(finding["configuration"], "italy")
        self.assertEqual(finding["config_file"], str(self.regional / "model.conf"))
        self.assertEqual(finding["section"], ["data"])
        self.assertEqual(finding["reference"], str(self.grid))
        self.assertEqual(finding["resolved_path"], str(self.grid))
        self.assertEqual(finding["status"], "MISSING")
        self.assertTrue(finding["corrective_action"])

    def test_invalid_syntax_does_not_echo_unrelated_values(self):
        (self.regional / "model.conf").write_text(
            "[invalid\nsecret = must-not-appear\n"
        )
        report = self.check()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertNotIn("must-not-appear", json.dumps(report))
        self.assertTrue(
            any(
                item["code"] == "configuration_parse_failed"
                for item in report["findings"]
            )
        )

    def test_invalid_filesystem_reference_keeps_configuration_context(self):
        content = self.documents["model.conf"].replace(str(self.grid), "/bad\x00path")
        (self.regional / "model.conf").write_text(content)
        report = self.check()
        finding = next(item for item in report["findings"] if item["key"] == "vs30file")
        self.assertEqual(finding["code"], "invalid_path_reference")
        self.assertEqual(finding["config_file"], str(self.regional / "model.conf"))

    def test_missing_key_and_unsupported_macro_are_incomplete(self):
        (self.regional / "model.conf").write_text(
            self.documents["model.conf"]
            .replace(f"vs30file = {self.grid}", "vs30file = <EVENT_DIR>/unknown.grd")
            .replace("ccf = correlation\n", "")
        )
        report = self.check()
        self.assertEqual(report["status"], "INCOMPLETE")
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("configuration_key_unavailable", codes)
        self.assertIn("unresolved_path_reference", codes)

    def test_missing_layer_checks_native_copy_source_without_materializing(self):
        (self.native / "layers/example.wkt").unlink()
        report = self.check()
        finding = next(
            item
            for item in report["findings"]
            if item["resolved_path"] == str(self.native / "layers/example.wkt")
        )
        self.assertEqual(finding["status"], "MISSING")
        self.assertEqual(finding["config_file"], str(self.regional / "select.conf"))
        self.assertEqual(finding["section"], ["layers", "example"])

    def test_enabled_amplification_requires_operator_review(self):
        with (self.regional / "model.conf").open("a") as stream:
            stream.write("apply_generic_amp_factors = true\n")
        report = self.check()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertTrue(
            any(
                item["code"] == "amplification_not_materialized"
                for item in report["findings"]
            )
        )

    def test_missing_configured_module_and_gmpe_set_report_context(self):
        with mock.patch.object(profile_checks, "_module_available", return_value=False):
            report = self.check()
        missing = [
            item
            for item in report["findings"]
            if item["code"] == "model_module_unavailable"
        ]
        self.assertTrue(any(item["key"] == "ground_motion" for item in missing))
        self.assertTrue(
            all(
                item["config_file"] == str(self.regional / "modules.conf")
                for item in missing
            )
        )
        (self.regional / "gmpe_sets.conf").write_text("[gmpe_sets]\n")
        self.assertTrue(
            any(item["code"] == "gmpe_set_missing" for item in self.check()["findings"])
        )

    def test_global_uses_materializer_overrides_not_template_grid(self):
        for target in (paths.vs30_grid_path(), paths.topo_grid_path()):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fixture")
        report = profile_checks.check_configuration("global")
        self.assertEqual(report["status"], "NO_KNOWN_BLOCKERS")
        grid = next(item for item in report["findings"] if item["key"] == "vs30file")
        self.assertEqual(grid["resolved_path"], str(paths.vs30_grid_path()))

    def test_check_does_not_write_or_run_native_helpers(self):
        before = {
            str(p): (p.stat().st_mtime_ns, p.read_bytes())
            for p in self.root.rglob("*")
            if p.is_file()
        }
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError("no commands")),
            mock.patch(
                "shakemap_service.native_profile.materialize_native_profile",
                side_effect=AssertionError("no profile creation"),
            ),
        ):
            self.check()
        after = {
            str(p): (p.stat().st_mtime_ns, p.read_bytes())
            for p in self.root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertFalse((self.root / "shakemap/.service").exists())

    def test_rest_cli_parity_and_exit_status(self):
        # Avoid application lifespan: the check itself needs no scheduler.
        response = TestClient(main.app).get("/configurations/italy/check")
        self.assertEqual(response.status_code, 200)
        expected = response.json()
        output = io.StringIO()
        with (
            mock.patch.object(cli, "_get_json", return_value=expected) as get,
            contextlib.redirect_stdout(output),
        ):
            result = cli.main(["check", "--configuration", "italy"])
        get.assert_called_once_with(
            cli.DEFAULT_SERVICE_URL, "/configurations/italy/check"
        )
        self.assertEqual(json.loads(output.getvalue()), expected)
        self.assertEqual(result, 0)
        for status, code in (("BLOCKED", 1), ("INCOMPLETE", 2)):
            with (
                mock.patch.object(cli, "_get_json", return_value={"status": status}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["check"]), code)

    def test_invalid_configuration_name_is_rejected(self):
        self.assertEqual(
            TestClient(main.app).get("/configurations/%2E%2E/check").status_code, 422
        )

    def test_helper_help_needs_no_environment_or_service(self):
        result = subprocess.run(
            ["bash", "scripts/check-shakemap.sh", "--help"],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Static profile", result.stdout)


if __name__ == "__main__":
    unittest.main()
