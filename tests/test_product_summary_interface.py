#!/usr/bin/env python3
"""Host tests for current service-owned product-manifest summaries."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from shakemap_service import cli, main, paths, product_manifest, public_views, status
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


class ProductSummaryInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_path_settings = paths.settings
        self.previous_status_settings = status.settings
        self.previous_view_settings = public_views.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )
        paths.settings = configured
        status.settings = configured
        public_views.settings = configured
        self.startup_recovery_patch = mock.patch.object(
            main.startup_recovery, "recover_interrupted_calculations", return_value=()
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

    def _current(self, event_id: str, lifecycle: str = "RUNNING"):
        accepted = self._accept(event_id)
        status.transition_to_running(accepted.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(accepted.internal_sequence).rename(
            paths.event_service_dir(event_id)
        )
        if lifecycle != "RUNNING":
            record_file = paths.event_status_file(event_id)
            payload = json.loads(record_file.read_text(encoding="utf-8"))
            payload["status"] = lifecycle
            payload["timestamps"]["completed_at"] = "2026-08-16T13:00:00.000000Z"
            payload["progress"]["phase"] = "record_finalization"
            payload["progress"]["phase_started_at"] = "2026-08-16T12:59:59.000000Z"
            payload["service_outcome"] = {
                "completed": True,
                "successful": lifecycle == "SUCCESS",
            }
            payload["failure"] = (
                None
                if lifecycle == "SUCCESS"
                else {
                    "phase": "record_finalization",
                    "code": "fixture",
                    "message": "fixture failure",
                }
            )
            record_file.write_text(json.dumps(payload), encoding="utf-8")
        record = status.read_current_record(event_id)
        if record is None:
            raise AssertionError("current record was not materialized")
        return record

    @staticmethod
    def _manifest(
        record: status.CalculationRecord,
        *,
        partial: bool,
        required_products: object,
        products: list[dict[str, object]],
    ) -> None:
        paths.event_manifest_file(record.event_id).write_text(
            json.dumps(
                {
                    "event_id": record.event_id,
                    "internal_sequence": record.internal_sequence,
                    "partial": partial,
                    "primary_reason": "fixture" if partial else None,
                    "inventory_failures": [
                        {"path": "omitted", "reason": "not in public summary"}
                    ],
                    "required_products": required_products,
                    "products": products,
                }
            ),
            encoding="utf-8",
        )

    def test_complete_manifest_maps_only_authoritative_summary_fields(self) -> None:
        record = self._current("complete", lifecycle="SUCCESS")
        self._manifest(
            record,
            partial=False,
            required_products={
                "paths": ["shake_result.hdf", "maps/pga.jpg"],
                "source": "configured",
                "passed": True,
                "checks": [
                    {
                        "path": "shake_result.hdf",
                        "size": 321,
                        "passed": True,
                        "reason": "generic checks passed",
                    },
                    {
                        "path": "maps/pga.jpg",
                        "size": 22,
                        "passed": True,
                        "reason": None,
                    },
                ],
            },
            products=[
                {
                    "path": "shake_result.hdf",
                    "size": 321,
                    "sha256": "a" * 64,
                },
                {
                    "path": "maps/pga.jpg",
                    "size": 22,
                    "sha256": "b" * 64,
                },
            ],
        )

        with (
            mock.patch.object(
                public_views.os,
                "walk",
                side_effect=AssertionError("native products were walked"),
            ),
            mock.patch.object(
                product_manifest,
                "_inventory_products",
                side_effect=AssertionError("native products were inventoried"),
            ),
            TestClient(main.app) as client,
        ):
            response = client.get("/events/complete/products")

        self.assertEqual(response.status_code, 200)
        current = response.json()["current"]
        self.assertEqual(current["status"], "SUCCESS")
        self.assertTrue(current["products_ready"])
        self.assertEqual(current["manifest_state"], "complete")
        self.assertEqual(
            current["required_products"],
            [
                {
                    "path": "shake_result.hdf",
                    "passed": True,
                    "reason": "generic checks passed",
                },
                {"path": "maps/pga.jpg", "passed": True, "reason": None},
            ],
        )
        self.assertEqual(
            current["products"],
            [
                {"path": "shake_result.hdf", "size_bytes": 321},
                {"path": "maps/pga.jpg", "size_bytes": 22},
            ],
        )
        self.assertNotIn("sha256", response.text)
        self.assertNotIn("inventory_failures", response.text)
        self.assertEqual(
            current["shared_paths"],
            {
                "products": (
                    "/operator/runtime/shakemap/products/complete/current/products"
                ),
                "product_manifest": (
                    "/operator/runtime/shakemap/.service/events/complete/"
                    "product-manifest.json"
                ),
            },
        )

    def test_partial_unavailable_and_lifecycle_flags_are_independent(self) -> None:
        partial = self._current("partial")
        self._manifest(
            partial,
            partial=True,
            required_products=None,
            products=[{"path": "partial.bin", "size": 8, "sha256": "c" * 64}],
        )
        unavailable = self._current("unavailable", lifecycle="FAILED")
        success_without_manifest = self._current("success-no-manifest", lifecycle="SUCCESS")

        with TestClient(main.app) as client:
            partial_response = client.get("/events/partial/products").json()["current"]
            unavailable_response = client.get(
                "/events/unavailable/products"
            ).json()["current"]
            success_response = client.get(
                "/events/success-no-manifest/products"
            ).json()["current"]

        self.assertEqual(partial_response["manifest_state"], "partial")
        self.assertEqual(partial_response["required_products"], [])
        self.assertEqual(
            partial_response["products"],
            [{"path": "partial.bin", "size_bytes": 8}],
        )
        self.assertFalse(partial_response["products_ready"])
        self.assertEqual(unavailable_response["manifest_state"], "unavailable")
        self.assertEqual(unavailable_response["required_products"], [])
        self.assertEqual(unavailable_response["products"], [])
        self.assertIsNone(
            unavailable_response["shared_paths"]["product_manifest"]
        )
        self.assertFalse(unavailable_response["products_ready"])
        self.assertEqual(success_response["manifest_state"], "unavailable")
        self.assertTrue(success_response["products_ready"])
        self.assertEqual(unavailable.status, "FAILED")
        self.assertEqual(success_without_manifest.status, "SUCCESS")

    def test_queued_and_archive_only_ids_have_no_current_summary(self) -> None:
        self._accept("queued-only")
        archive = paths.event_archive_dir(
            "archive-only",
            "20260816T120000.000000Z",
        )
        (archive / "products").mkdir(parents=True)
        with TestClient(main.app) as client:
            queued = client.get("/events/queued-only/products")
            archived = client.get("/events/archive-only/products")
            unknown = client.get("/events/unknown/products")
        self.assertEqual(queued.status_code, 200)
        self.assertIsNone(queued.json()["current"])
        self.assertEqual(archived.status_code, 200)
        self.assertIsNone(archived.json()["current"])
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"], "request_rejected")

    def test_malformed_or_identity_mismatched_manifest_is_json_500(self) -> None:
        cases = (
            ("malformed", "{broken"),
            (
                "wrong-identity",
                json.dumps(
                    {
                        "event_id": "other",
                        "internal_sequence": 2,
                        "partial": True,
                        "required_products": None,
                        "products": [],
                    }
                ),
            ),
            (
                "unsafe-path",
                json.dumps(
                    {
                        "event_id": "unsafe-path",
                        "internal_sequence": 3,
                        "partial": True,
                        "required_products": None,
                        "products": [{"path": "../escape", "size": 1}],
                    }
                ),
            ),
        )
        for event_id, payload in cases:
            record = self._current(event_id)
            if event_id == "wrong-identity":
                document = json.loads(payload)
                document["internal_sequence"] = record.internal_sequence
                payload = json.dumps(document)
            elif event_id == "unsafe-path":
                document = json.loads(payload)
                document["internal_sequence"] = record.internal_sequence
                payload = json.dumps(document)
            paths.event_manifest_file(event_id).write_text(payload, encoding="utf-8")

        with TestClient(main.app, raise_server_exceptions=False) as client:
            for event_id, _ in cases:
                with self.subTest(event_id=event_id):
                    response = client.get(f"/events/{event_id}/products")
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.json()["error"], "service_failure")
                    self.assertNotIn(self.temporary.name, response.text)

    def test_rest_and_cli_products_are_semantically_identical(self) -> None:
        self._accept("parity")
        with TestClient(main.app) as client:
            expected = client.get("/events/parity/products").json()

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
                    ["--url", "http://service.test", "products", "parity"]
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        self.assertEqual(error.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
