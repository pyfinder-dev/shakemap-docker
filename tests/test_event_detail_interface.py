#!/usr/bin/env python3
"""Host tests for event-detail and retained-archive projections."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from fastapi.testclient import TestClient

from shakemap_service import cli, main, paths, public_views, status
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


class EventDetailInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_path_settings = paths.settings
        self.previous_status_settings = status.settings
        self.previous_view_settings = public_views.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
            max_concurrent=2,
        )
        paths.settings = configured
        status.settings = configured
        public_views.settings = configured
        self.startup_recovery_patch = mock.patch.object(
            main.startup_recovery,
            "recover_interrupted_calculations",
            return_value=(),
        )
        self.startup_recovery_patch.start()

    def tearDown(self) -> None:
        self.startup_recovery_patch.stop()
        public_views.settings = self.previous_view_settings
        status.settings = self.previous_status_settings
        paths.settings = self.previous_path_settings
        self.temporary.cleanup()

    @staticmethod
    def _accept(event_id: str):
        return accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(event_id.encode("utf-8")))],
        )

    def _promote_running(self, event_id: str):
        accepted = self._accept(event_id)
        status.transition_to_running(accepted.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(accepted.internal_sequence).rename(
            paths.event_service_dir(event_id)
        )
        record = status.read_current_record(event_id)
        if record is None:
            raise AssertionError("current record was not promoted")
        progress = dict(record.progress)
        progress.update(
            phase="native_execution",
            phase_started_at="2026-08-16T12:00:01.000000Z",
        )
        return status.update_current_record(event_id, progress=progress)

    def _archive(
        self,
        event_id: str,
        timestamp: str,
        *,
        products: bool,
        service: bool,
        lifecycle: str = "FAILED",
        provenance: bool = False,
    ) -> Path:
        archive = paths.event_archive_dir(event_id, timestamp)
        archive.mkdir(parents=True)
        if products:
            (archive / "products").mkdir()
        if service:
            accepted = self._accept(event_id)
            status.transition_to_running(accepted.internal_sequence)
            status.transition_to_failed(
                accepted.internal_sequence,
                "retained fixture failure",
            )
            record_file = paths.queue_status_file(accepted.internal_sequence)
            if lifecycle == "SUCCESS":
                payload = json.loads(record_file.read_text(encoding="utf-8"))
                payload["status"] = "SUCCESS"
                payload["failure"] = None
                payload["service_outcome"] = {
                    "completed": True,
                    "successful": True,
                }
                record_file.write_text(json.dumps(payload), encoding="utf-8")
            paths.queue_entry_dir(accepted.internal_sequence).rename(
                archive / "service"
            )
            if provenance:
                (archive / "service/provenance.json").write_text(
                    json.dumps(
                        {
                            "event_id": event_id,
                            "internal_sequence": accepted.internal_sequence,
                            "complete": {"retained": True},
                        }
                    ),
                    encoding="utf-8",
                )
        return archive

    def _rich_provenance(
        self,
        event_id: str,
        internal_sequence: int,
    ) -> dict[str, object]:
        private_profile = str(paths.service_root() / ".service/events" / event_id / "profile")
        return {
            "event_id": event_id,
            "internal_sequence": internal_sequence,
            "configuration": {
                "selected": "global",
                "materialization": {
                    "materialized": True,
                    "source_directory": private_profile,
                    "profile_helper": {
                        "return_code": 0,
                        "output": f"materialized from {private_profile}/config",
                    },
                    "strec_helper": {
                        "return_code": 0,
                        "command": ["helper", "/opt/shakemap/bin/strec"],
                    },
                },
                "profile_files": [
                    {"path": "config.conf", "sha256": "a" * 64},
                ],
            },
            "software_identity": {
                "schema_version": "1.0",
                "immutable_image": {
                    "available": True,
                    "installed": {
                        "shakemap_distribution_version": "4.4.9",
                        "dependency_inventory_path": "/opt/build/dependencies.txt",
                        "dependency_inventory_sha256": "b" * 64,
                        "mapping_compatibility": {
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
                        },
                        "strec": {
                            "distribution_version": "2.3.14",
                            "database_path": "/site-packages/strec/database.db",
                            "database_link": "/opt/support/strec/database.db",
                            "database_sha256": "f" * 64,
                        },
                        "slab2": {
                            "source_url": "https://example.test/slab2.zip",
                            "source_archive_path": "/opt/support/slab2/source.zip",
                            "source_archive_sha256": "1" * 64,
                            "source_manifest_path": "/opt/support/slab2/source.json",
                            "source_manifest_sha256": "2" * 64,
                            "installed_files_manifest_path": "/opt/support/slab2/files.json",
                            "installed_files_manifest_sha256": "3" * 64,
                            "slabs_dir": "/opt/support/slab2/slabs",
                        },
                    },
                    "built_at_utc": "2026-08-16T10:00:00Z",
                },
                "deployment": {"available": False, "source": "unavailable"},
            },
            "native_execution": {
                "command": ["shake", event_id, "select", "assemble"],
                "started_at": "2026-08-16T12:00:01Z",
                "completed_at": "2026-08-16T12:00:02.123456Z",
                "exit_code": 1,
            },
            "warnings": [f"diagnostic from {private_profile}/warning.log"],
            "failure": {
                "phase": "native_execution",
                "message": "helper failed at /opt/shakemap/bin/shake",
            },
            "timestamps": {
                "accepted_at": "2026-08-16T12:00:00Z",
                "started_at": "2026-08-16T12:00:01Z",
                "native_completed_at": "2026-08-16T12:00:02.123456Z",
                "validated_at": None,
                "terminal_at": "2026-08-16T12:00:03Z",
            },
            "locations": {
                "products": f"/operator/runtime/shakemap/products/{event_id}",
            },
            "large_datasets": {
                "global": {
                    "vs30": {
                        "path": "/operator/runtime/shakemap/data/global/vs30/grid.grd",
                    },
                },
            },
            "unrelated": {"retained": True},
        }

    def test_current_and_repeated_queue_rows_share_polling_facts(self) -> None:
        current = self._promote_running("repeat")
        second = self._accept("repeat")
        third = self._accept("repeat")
        provenance = {
            "event_id": "repeat",
            "internal_sequence": current.internal_sequence,
            "nested": {"complete": [True, 4]},
        }
        paths.event_provenance_file("repeat").write_text(
            json.dumps(provenance),
            encoding="utf-8",
        )

        with TestClient(main.app) as client:
            detail = client.get("/events/repeat")
            collection = client.get("/events")

        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(
            [row["internal_sequence"] for row in body["jobs"]],
            [current.internal_sequence, second.internal_sequence, third.internal_sequence],
        )
        self.assertEqual(body["jobs"][0]["provenance"], provenance)
        self.assertEqual(
            body["jobs"][0]["shared_paths"]["provenance"],
            "/operator/runtime/shakemap/.service/events/repeat/provenance.json",
        )
        self.assertIsNone(body["jobs"][1]["provenance"])
        self.assertEqual(
            [row["waiting_reason"] for row in body["jobs"][1:]],
            ["same_event_active", "same_event_active"],
        )

        compact = {
            row["internal_sequence"]: row for row in collection.json()["jobs"]
        }
        for detail_row in body["jobs"]:
            row = compact[detail_row["internal_sequence"]]
            for key in (
                "status",
                "job_completed",
                "products_ready",
                "queue_position",
                "waiting_reason",
                "phase",
            ):
                self.assertEqual(detail_row[key], row[key])
            self.assertEqual(
                detail_row["timestamps"]["submitted_at"],
                row["submitted_at"],
            )

    def test_archives_are_exact_newest_first_and_truthfully_nullable(self) -> None:
        event_id = "quake-long:α"
        complete = self._archive(
            event_id,
            "20260816T120000.000001Z",
            products=True,
            service=True,
            lifecycle="SUCCESS",
            provenance=True,
        )
        products_only = self._archive(
            event_id,
            "20260816T120000.000002Z",
            products=True,
            service=False,
        )
        service_only = self._archive(
            event_id,
            "20260816T120000.000003Z",
            products=False,
            service=True,
            provenance=True,
        )
        self._archive(
            event_id + "-extra",
            "20260816T120000.000004Z",
            products=True,
            service=False,
        )

        with (
            mock.patch.object(
                public_views.os,
                "walk",
                side_effect=AssertionError("native product tree was walked"),
            ),
            TestClient(main.app) as client,
        ):
            response = client.get("/events/" + quote(event_id, safe=""))

        self.assertEqual(response.status_code, 200)
        archives = response.json()["archives"]
        self.assertEqual(
            [row["archived_at"] for row in archives],
            [
                "2026-08-16T12:00:00.000003Z",
                "2026-08-16T12:00:00.000002Z",
                "2026-08-16T12:00:00.000001Z",
            ],
        )
        newest = archives[0]
        self.assertEqual(newest["status"], "FAILED")
        self.assertFalse(newest["products_ready"])
        self.assertIsNone(newest["shared_paths"]["products"])
        self.assertEqual(newest["provenance"]["complete"], {"retained": True})

        middle = archives[1]
        self.assertIsNone(middle["internal_sequence"])
        self.assertIsNone(middle["status"])
        self.assertIsNone(middle["provenance"])
        self.assertFalse(middle["products_ready"])
        self.assertIsNotNone(middle["shared_paths"]["products"])
        for name in ("provenance", "product_manifest", "service_log", "shake_log"):
            self.assertIsNone(middle["shared_paths"][name])

        oldest = archives[2]
        self.assertEqual(oldest["status"], "SUCCESS")
        self.assertTrue(oldest["products_ready"])
        self.assertIn(complete.name, oldest["shared_paths"]["archive"])
        self.assertIn(products_only.name, middle["shared_paths"]["archive"])
        self.assertIn(service_only.name, newest["shared_paths"]["archive"])
        self.assertNotIn("/events/", oldest["shared_paths"]["provenance"])

    def test_current_and_archive_provenance_share_public_safe_projection(self) -> None:
        event_id = "projected"
        current = self._promote_running(event_id)
        current_provenance = self._rich_provenance(
            event_id,
            current.internal_sequence,
        )
        current_file = paths.event_provenance_file(event_id)
        current_file.write_text(json.dumps(current_provenance), encoding="utf-8")

        archive = self._archive(
            event_id,
            "20260816T120000.000001Z",
            products=True,
            service=True,
            provenance=True,
        )
        archived_status = json.loads(
            (archive / "service/status.json").read_text(encoding="utf-8")
        )
        archived_provenance = self._rich_provenance(
            event_id,
            archived_status["internal_sequence"],
        )
        archive_file = archive / "service/provenance.json"
        archive_file.write_text(json.dumps(archived_provenance), encoding="utf-8")
        current_bytes = current_file.read_bytes()
        archive_bytes = archive_file.read_bytes()

        with TestClient(main.app) as client:
            response = client.get("/events/projected")

        self.assertEqual(response.status_code, 200)
        projected_values = (
            response.json()["jobs"][0]["provenance"],
            response.json()["archives"][0]["provenance"],
        )
        for projected in projected_values:
            with self.subTest(sequence=projected["internal_sequence"]):
                materialization = projected["configuration"]["materialization"]
                self.assertNotIn("source_directory", materialization)
                rendered = json.dumps(projected)
                self.assertNotIn(str(paths.service_root()), rendered)
                self.assertNotIn("/opt/", rendered)
                self.assertNotIn("/site-packages/strec/database.db", rendered)
                self.assertEqual(
                    projected["native_execution"]["command"],
                    ["shake", event_id, "select", "assemble"],
                )
                self.assertEqual(
                    projected["configuration"]["profile_files"][0]["path"],
                    "config.conf",
                )
                self.assertEqual(
                    projected["locations"]["products"],
                    f"/operator/runtime/shakemap/products/{event_id}",
                )
                self.assertEqual(
                    projected["large_datasets"]["global"]["vs30"]["path"],
                    "/operator/runtime/shakemap/data/global/vs30/grid.grd",
                )
                self.assertEqual(projected["unrelated"], {"retained": True})
                installed = projected["software_identity"]["immutable_image"]["installed"]
                self.assertNotIn("dependency_inventory_path", installed)
                self.assertEqual(installed["dependency_inventory_sha256"], "b" * 64)

        self.assertEqual(current_file.read_bytes(), current_bytes)
        self.assertEqual(archive_file.read_bytes(), archive_bytes)

    def test_malformed_current_and_archive_provenance_timestamps_fail_closed(
        self,
    ) -> None:
        current = self._promote_running("bad-current-time")
        current_payload = {
            "event_id": current.event_id,
            "internal_sequence": current.internal_sequence,
            "timestamps": {"accepted_at": "20260816T120000Z"},
        }
        paths.event_provenance_file(current.event_id).write_text(
            json.dumps(current_payload),
            encoding="utf-8",
        )
        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.get("/events/bad-current-time")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["details"][0]["source"], "current")
        self.assertNotIn("20260816T120000Z", response.text)

        archive = self._archive(
            "bad-archive-time",
            "20260816T120000.000001Z",
            products=False,
            service=True,
            provenance=True,
        )
        archived = json.loads(
            (archive / "service/status.json").read_text(encoding="utf-8")
        )
        (archive / "service/provenance.json").write_text(
            json.dumps(
                {
                    "event_id": "bad-archive-time",
                    "internal_sequence": archived["internal_sequence"],
                    "native_execution": {
                        "started_at": "2026-08-16 12:00:00Z",
                        "completed_at": "2026-08-16T12:00:01Z",
                    },
                }
            ),
            encoding="utf-8",
        )
        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.get("/events/bad-archive-time")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["details"][0]["source"], "archive")
        self.assertNotIn("2026-08-16 12:00:00Z", response.text)

    def test_encoded_ids_are_decoded_once_and_preserved(self) -> None:
        event_ids = ("space value", "ümlaut:α")
        for event_id in event_ids:
            self._accept(event_id)
        self._accept("literal%2Fvalue")
        with TestClient(main.app) as client:
            for event_id in event_ids:
                with self.subTest(event_id=event_id):
                    response = client.get("/events/" + quote(event_id, safe=""))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["event_id"], event_id)
        self.assertEqual(
            main.get_event("literal%2Fvalue")["event_id"],
            "literal%2Fvalue",
        )

    def test_failed_status_rest_and_cli_are_semantically_identical(self) -> None:
        record = self._promote_running("failed-parity")
        record_file = paths.event_status_file(record.event_id)
        payload = json.loads(record_file.read_text(encoding="utf-8"))
        payload["status"] = "FAILED"
        payload["timestamps"]["completed_at"] = "2026-08-16T12:01:00.000000Z"
        payload["failure"] = {
            "phase": "native_execution",
            "code": "native_exit",
            "message": "ShakeMap exited with code 1",
        }
        payload["service_outcome"] = {"completed": True, "successful": False}
        record_file.write_text(json.dumps(payload), encoding="utf-8")

        with TestClient(main.app) as client:
            rest = client.get("/events/failed-parity")

            def fetch(service_url: str, endpoint: str) -> object:
                self.assertEqual(service_url, "http://service.test")
                return client.get(endpoint).json()

            output = io.StringIO()
            error = io.StringIO()
            with (
                mock.patch.object(cli, "_get_json", side_effect=fetch),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                exit_code = cli.main(
                    ["--url", "http://service.test", "status", "failed-parity"]
                )

        self.assertEqual(rest.status_code, 200)
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), rest.json())
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(rest.json()["jobs"][0]["status"], "FAILED")

    def test_unknown_and_malformed_records_use_sanitized_json_errors(self) -> None:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            unknown = client.get("/events/unknown")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"], "request_rejected")

        current = self._promote_running("bad-provenance")
        paths.event_provenance_file(current.event_id).write_text(
            "{broken",
            encoding="utf-8",
        )
        with TestClient(main.app, raise_server_exceptions=False) as client:
            malformed = client.get("/events/bad-provenance")
        self.assertEqual(malformed.status_code, 500)
        self.assertEqual(malformed.json()["error"], "service_failure")
        self.assertNotIn(self.temporary.name, malformed.text)

    def test_malformed_archive_status_and_name_fail_closed(self) -> None:
        archive = self._archive(
            "archive-error",
            "20260816T120000.000001Z",
            products=False,
            service=True,
        )
        (archive / "service/status.json").write_text("{broken", encoding="utf-8")
        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.get("/events/archive-error")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["details"][0]["source"], "archive")

        (paths.archive_dir() / "malformed-name").mkdir()
        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.get("/events/archive-error")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["details"][0]["source"], "archive")


class EventDetailCliTests(unittest.TestCase):
    def test_status_and_products_percent_encode_once(self) -> None:
        calls: list[tuple[str, str]] = []

        def fetch(service_url: str, endpoint: str) -> object:
            calls.append((service_url, endpoint))
            return {"ok": True}

        for command, suffix in (("status", ""), ("products", "/products")):
            output = io.StringIO()
            error = io.StringIO()
            with (
                mock.patch.object(cli, "_get_json", side_effect=fetch),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                exit_code = cli.main(
                    ["--url", "http://service.test/root", command, "space ü:%2F"]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue()), {"ok": True})
            self.assertEqual(error.getvalue(), "")
            self.assertEqual(
                calls[-1],
                (
                    "http://service.test/root",
                    "/events/space%20%C3%BC%3A%252F" + suffix,
                ),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
