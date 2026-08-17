#!/usr/bin/env python3
"""Host tests for lightweight service-information REST and CLI views."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from fastapi.testclient import TestClient

from shakemap_service import (
    cli,
    main,
    paths,
    preparation,
    readiness,
    required_products,
    service_information,
)
from shakemap_service.config import MODULE_PLAN, Settings


NOT_RECORDED = "deployment readiness has not been recorded"


def available_identity(version: str = "4.4.9") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "immutable_image": {
            "available": True,
            "upstream": {"release_tag": "v4.4.9", "source_commit": "a" * 40},
            "installed": {"shakemap_distribution_version": version},
        },
        "deployment": {"available": True, "image_id": "sha256:" + "b" * 64},
    }


def unavailable_identity() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "immutable_image": {
            "available": False,
            "reason": "Recorded build identity unavailable",
        },
        "deployment": {"available": False},
    }


class ServiceInformationRestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_path_settings = paths.settings
        self.previous_information_settings = service_information.settings
        selected = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
            max_concurrent=7,
        )
        paths.settings = selected
        service_information.settings = selected

    def tearDown(self) -> None:
        service_information.settings = self.previous_information_settings
        paths.settings = self.previous_path_settings
        self.temporary.cleanup()

    def test_health_and_config_have_exact_shared_fail_closed_readiness(self) -> None:
        identity = available_identity("4.4.9")
        with mock.patch.object(
            service_information,
            "service_identity",
            return_value=identity,
        ), TestClient(main.app) as client:
            health = client.get("/healthz")
            config = client.get("/config")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(
            health.json(),
            {
                "ready": False,
                "reason": NOT_RECORDED,
                "shakemap_version": "4.4.9",
            },
        )
        self.assertEqual(
            config.json(),
            {
                "identity": identity,
                "shakemap_version": "4.4.9",
                "module_plan": list(MODULE_PLAN),
                "default_configuration": "global",
                "maximum_running": 7,
                "shared_service_root": "/operator/runtime/shakemap",
                "required_products": {"mode": "derived", "paths": []},
                "readiness": {"ready": False, "reason": NOT_RECORDED},
            },
        )
        self.assertEqual(
            config.json()["readiness"],
            {key: health.json()[key] for key in ("ready", "reason")},
        )

    def test_health_and_config_use_one_matching_identity_snapshot_each(self) -> None:
        identity = available_identity()
        loader = mock.Mock(return_value=identity)
        probe = mock.Mock(return_value={"ready": True, "reason": None})
        with (
            mock.patch.object(service_information, "service_identity", loader),
            mock.patch.object(readiness, "read_readiness", probe),
        ):
            health = main.healthz()
            self.assertEqual(loader.call_count, 1)
            self.assertIs(probe.call_args.args[0], identity)
            loader.reset_mock()
            probe.reset_mock()
            config = main.get_config()
            self.assertEqual(loader.call_count, 1)
            self.assertIs(probe.call_args.args[0], identity)
        self.assertEqual(
            {key: health[key] for key in ("ready", "reason")},
            config["readiness"],
        )
        self.assertEqual(config["readiness"], {"ready": True, "reason": None})

    def test_unavailable_identity_has_null_version_without_declared_fallback(self) -> None:
        identity = unavailable_identity()
        with mock.patch.object(
            service_information,
            "service_identity",
            return_value=identity,
        ), TestClient(main.app) as client:
            health = client.get("/healthz")
            config = client.get("/config")

        self.assertIsNone(health.json()["shakemap_version"])
        self.assertIsNone(config.json()["shakemap_version"])
        self.assertNotIn("4.4.9", json.dumps(health.json()))
        self.assertEqual(config.json()["identity"], identity)

    def test_malformed_available_identity_is_a_sanitized_json_failure(self) -> None:
        malformed = {
            "schema_version": "1.0",
            "immutable_image": {"available": True},
            "deployment": {"available": False},
        }
        with mock.patch.object(
            service_information,
            "service_identity",
            return_value=malformed,
        ), TestClient(main.app, raise_server_exceptions=False) as client:
            for endpoint in ("/healthz", "/config"):
                with self.subTest(endpoint=endpoint):
                    response = client.get(endpoint)
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(
                        response.json(),
                        {
                            "error": "service_failure",
                            "message": "Service information is unavailable",
                            "details": [],
                        },
                    )

    def test_config_projects_private_identity_paths_without_mutating_identity(
        self,
    ) -> None:
        identity = {
            "schema_version": "1.0",
            "immutable_image": {
                "available": True,
                "upstream": {
                    "repository_url": "https://github.com/usgs/shakemap.git",
                    "release_tag": "v4.4.9",
                    "source_commit": "a" * 40,
                },
                "installed": {
                    "shakemap_distribution_version": "4.4.9",
                    "python_version": "3.12.7",
                    "dependency_inventory_path": "/opt/build/dependencies.txt",
                    "dependency_inventory_sha256": "b" * 64,
                    "mapping_compatibility": {
                        "policy": "resolved_release_conda_lock",
                        "locked_version": "3.10.8",
                        "source_lock_path": "/opt/source/conda-lock.yml",
                        "source_lock_sha256": "c" * 64,
                        "record_path": "/opt/build/mapping.json",
                        "record_sha256": "d" * 64,
                    },
                },
                "support": {
                    "natural_earth": {
                        "tag": "v5.1.2",
                        "manifest_path": "/opt/support/natural-earth.json",
                        "manifest_sha256": "e" * 64,
                        "cartopy_data_dir": "/opt/support/cartopy",
                        "file_count": 20,
                    },
                    "strec": {
                        "distribution_version": "2.3.14",
                        "database_path": "/site-packages/strec/data/database.db",
                        "database_link": "/opt/support/strec/database.db",
                        "database_size": 10,
                        "database_sha256": "f" * 64,
                    },
                    "slab2": {
                        "version": "Slab2",
                        "source_url": "https://example.test/slab2.zip",
                        "source_archive_path": "/opt/support/slab2/source.zip",
                        "source_archive_sha256": "1" * 64,
                        "source_manifest_path": "/opt/support/slab2/source.json",
                        "source_manifest_sha256": "2" * 64,
                        "installed_files_manifest_path": "/opt/support/slab2/files.json",
                        "installed_files_manifest_sha256": "3" * 64,
                        "slabs_dir": "/opt/support/slab2/slabs",
                        "file_count": 108,
                    },
                },
                "built_at_utc": "2026-08-16T10:20:00Z",
            },
            "deployment": {
                "available": True,
                "image_id": "sha256:" + "4" * 64,
                "source": "runtime_environment",
            },
        }
        original = json.loads(json.dumps(identity))
        with mock.patch.object(
            service_information,
            "service_identity",
            return_value=identity,
        ), TestClient(main.app) as client:
            response = client.get("/config")

        self.assertEqual(response.status_code, 200)
        projected = response.json()["identity"]
        installed = projected["immutable_image"]["installed"]
        support = projected["immutable_image"]["support"]
        self.assertNotIn("dependency_inventory_path", installed)
        self.assertNotIn("source_lock_path", installed["mapping_compatibility"])
        self.assertNotIn("record_path", installed["mapping_compatibility"])
        self.assertNotIn("manifest_path", support["natural_earth"])
        self.assertNotIn("cartopy_data_dir", support["natural_earth"])
        self.assertNotIn("database_path", support["strec"])
        self.assertNotIn("database_link", support["strec"])
        for name in (
            "source_archive_path",
            "source_manifest_path",
            "installed_files_manifest_path",
            "slabs_dir",
        ):
            self.assertNotIn(name, support["slab2"])
        self.assertEqual(installed["dependency_inventory_sha256"], "b" * 64)
        self.assertEqual(support["natural_earth"]["tag"], "v5.1.2")
        self.assertEqual(support["strec"]["database_size"], 10)
        self.assertEqual(support["slab2"]["source_url"], "https://example.test/slab2.zip")
        self.assertEqual(projected["deployment"], identity["deployment"])
        self.assertEqual(
            response.json()["shared_service_root"],
            "/operator/runtime/shakemap",
        )
        self.assertNotIn("/opt/", json.dumps(projected))
        self.assertEqual(identity, original)

    def test_unavailable_identity_reason_omits_private_manifest_path(self) -> None:
        identity = {
            "schema_version": "1.0",
            "immutable_image": {
                "available": False,
                "manifest_path": "/opt/shakemap-build/identity.json",
                "reason": (
                    "Recorded build identity unavailable: "
                    "/opt/shakemap-build/identity.json could not be read"
                ),
            },
            "deployment": {"available": False},
        }
        original = json.loads(json.dumps(identity))
        with mock.patch.object(
            service_information,
            "service_identity",
            return_value=identity,
        ), TestClient(main.app) as client:
            response = client.get("/config")

        projected = response.json()["identity"]["immutable_image"]
        self.assertNotIn("manifest_path", projected)
        self.assertNotIn("/opt/", projected["reason"])
        self.assertIn("Recorded build identity unavailable", projected["reason"])
        self.assertEqual(identity, original)

    def test_malformed_identity_timestamp_is_a_sanitized_config_failure(self) -> None:
        identity = available_identity()
        identity["immutable_image"]["built_at_utc"] = "20260816T102000Z"
        with mock.patch.object(
            service_information,
            "service_identity",
            return_value=identity,
        ), TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.get("/config")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "service_failure")
        self.assertNotIn("20260816T102000Z", response.text)

    def test_config_maps_nonempty_code_owned_required_product_policy(self) -> None:
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="relative/runtime",
            max_concurrent=3,
            required_products=("shake_result.hdf", "maps/pga.jpg"),
        )
        with mock.patch.object(
            service_information,
            "service_identity",
            return_value=available_identity(),
        ):
            response = service_information.build_config_response(configured)

        self.assertEqual(response["maximum_running"], 3)
        self.assertEqual(response["shared_service_root"], "relative/runtime/shakemap")
        self.assertEqual(response["default_configuration"], "global")
        self.assertEqual(response["module_plan"], list(MODULE_PLAN))
        self.assertEqual(
            response["required_products"],
            {
                "mode": "configured",
                "paths": ["shake_result.hdf", "maps/pga.jpg"],
            },
        )

    def test_configuration_discovery_is_names_only_sorted_and_immediate(self) -> None:
        regional = paths.regional_data_dir()
        (regional / "turkiye" / "nested").mkdir(parents=True)
        (regional / "greece").mkdir()
        (regional / "global").mkdir()
        (regional / "ordinary.txt").write_text("ignored", encoding="utf-8")
        (regional / "linked").symlink_to(regional / "greece", target_is_directory=True)

        with TestClient(main.app) as client:
            response = client.get("/configurations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "default": "global",
                "configurations": ["global", "greece", "turkiye"],
            },
        )
        rendered = json.dumps(response.json())
        for forbidden in (
            str(regional),
            "profile",
            "loadable",
            "validation",
            "coverage",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_missing_regional_root_is_global_only(self) -> None:
        with TestClient(main.app) as client:
            response = client.get("/configurations")
        self.assertEqual(
            response.json(),
            {"default": "global", "configurations": ["global"]},
        )

    def test_unsafe_root_and_discovery_failures_are_sanitized(self) -> None:
        regional = paths.regional_data_dir()
        regional.parent.mkdir(parents=True)
        regional.write_text("not a directory", encoding="utf-8")
        with TestClient(main.app, raise_server_exceptions=False) as client:
            malformed = client.get("/configurations")
        self.assertEqual(malformed.status_code, 500)
        self.assertEqual(malformed.json()["error"], "service_failure")
        self.assertNotIn(str(regional), malformed.text)

        regional.unlink()
        regional.mkdir()
        with mock.patch.object(
            service_information.os,
            "scandir",
            side_effect=PermissionError("private path detail"),
        ), TestClient(main.app, raise_server_exceptions=False) as client:
            unscannable = client.get("/configurations")
        self.assertEqual(unscannable.status_code, 500)
        self.assertEqual(unscannable.json()["error"], "service_failure")
        self.assertNotIn("private path detail", unscannable.text)

    def test_unsafe_configuration_name_fails_without_partial_output(self) -> None:
        regional = paths.regional_data_dir()
        regional.mkdir(parents=True)
        (regional / "valid").mkdir()
        (regional / "bad\nname").mkdir()

        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.get("/configurations")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "service_failure")
        self.assertNotIn("valid", response.text)
        self.assertNotIn("bad", response.text)

    def test_unsafe_named_regular_file_is_not_a_configuration(self) -> None:
        regional = paths.regional_data_dir()
        regional.mkdir(parents=True)
        (regional / "bad\nname").write_text("ignored", encoding="utf-8")

        with TestClient(main.app) as client:
            response = client.get("/configurations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"default": "global", "configurations": ["global"]},
        )

    def test_discovery_does_not_inspect_child_content_data_or_native_tools(self) -> None:
        regional = paths.regional_data_dir()
        (regional / "greece").mkdir(parents=True)
        child = regional / "greece" / "config.conf"
        child.write_text("native content", encoding="utf-8")

        with (
            mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("child content read"),
            ),
            mock.patch.object(
                preparation,
                "inspect_data_assets",
                side_effect=AssertionError("data inspected"),
            ),
            mock.patch.object(
                required_products,
                "resolve_required_products",
                side_effect=AssertionError("products resolved"),
            ),
            mock.patch(
                "subprocess.run",
                side_effect=AssertionError("native command invoked"),
            ),
        ):
            response = service_information.build_configurations_response()

        self.assertEqual(
            response,
            {"default": "global", "configurations": ["global", "greece"]},
        )

    def test_obsolete_routes_are_absent_and_submission_is_fail_closed(self) -> None:
        with TestClient(main.app) as client:
            profiles = client.get("/config/profiles")
            legacy_submission = client.post("/events/submit")
            contracted_submission = client.post("/events")
        self.assertEqual(profiles.status_code, 404)
        self.assertEqual(legacy_submission.status_code, 405)
        self.assertEqual(contracted_submission.status_code, 503)
        self.assertEqual(contracted_submission.json()["error"], "service_unavailable")

    def test_persisted_readiness_controls_submission_before_acceptance(self) -> None:
        identity = available_identity()
        multipart = {"content-type": "multipart/form-data; boundary=x"}
        with (
            TestClient(main.app) as client,
            mock.patch.object(main.Request, "form", side_effect=AssertionError) as form,
            mock.patch.object(main.submission, "accept_request") as accept,
        ):
            readiness._record_not_ready("activation failed")
            loader = mock.Mock(side_effect=AssertionError)
            with mock.patch("shakemap_service.build_identity.service_identity", loader):
                not_ready = client.post("/events", headers=multipart)
            self.assertEqual(not_ready.status_code, 503)
            self.assertEqual(not_ready.json()["message"], "activation failed")
            loader.assert_not_called()

            readiness._record_ready(identity)
            changed = available_identity()
            changed["deployment"]["image_id"] = "sha256:" + "c" * 64
            with mock.patch(
                "shakemap_service.build_identity.service_identity", return_value=changed
            ):
                mismatch = client.post("/events", headers=multipart)
            self.assertEqual(mismatch.status_code, 503)
            self.assertEqual(mismatch.json()["message"], readiness.MISMATCH)

            with mock.patch(
                "shakemap_service.build_identity.service_identity", return_value=identity
            ):
                ordinary = client.post("/events", json={})
            self.assertEqual(ordinary.status_code, 400)
            self.assertIn("Content-Type must be multipart/form-data", ordinary.text)
            form.assert_not_called()
            accept.assert_not_called()


class ServiceInformationCliTests(unittest.TestCase):
    def test_commands_use_default_and_override_urls_with_json_only_stdout(self) -> None:
        calls: list[tuple[str, str]] = []

        def fetch(service_url: str, endpoint: str) -> object:
            calls.append((service_url, endpoint))
            return {"endpoint": endpoint}

        cases = (
            (["health"], cli.DEFAULT_SERVICE_URL, "/healthz"),
            (
                ["--url", "http://service.test/base/", "config"],
                "http://service.test/base/",
                "/config",
            ),
            (["configurations"], cli.DEFAULT_SERVICE_URL, "/configurations"),
        )
        with mock.patch.object(cli, "_get_json", side_effect=fetch):
            for arguments, expected_url, endpoint in cases:
                output = io.StringIO()
                error = io.StringIO()
                with (
                    self.subTest(arguments=arguments),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(error),
                ):
                    self.assertEqual(cli.main(arguments), 0)
                    self.assertEqual(json.loads(output.getvalue()), {"endpoint": endpoint})
                    self.assertEqual(error.getvalue(), "")
                    self.assertEqual(calls[-1], (expected_url, endpoint))

    def test_transport_failure_uses_stderr_and_nonzero_exit(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with (
            mock.patch.object(cli, "_get_json", side_effect=URLError("offline")),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            exit_code = cli.main(["health"])
        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("offline", error.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
