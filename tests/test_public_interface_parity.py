#!/usr/bin/env python3
"""Host parity tests for the eight public REST and CLI operation pairs."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from shakemap_service import cli, main, paths, service_information, status
from shakemap_service.config import Settings


def _identity() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "immutable_image": {
            "available": True,
            "installed": {"shakemap_distribution_version": "4.4.9"},
        },
        "deployment": {"available": False},
    }


class PublicInterfaceParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_paths_settings = paths.settings
        self.previous_status_settings = status.settings
        self.previous_information_settings = service_information.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )
        paths.settings = configured
        status.settings = configured
        service_information.settings = configured

    def tearDown(self) -> None:
        service_information.settings = self.previous_information_settings
        status.settings = self.previous_status_settings
        paths.settings = self.previous_paths_settings
        self.temporary.cleanup()

    def _run_cli(self, arguments: list[str]) -> object:
        output = io.StringIO()
        error = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            exit_code = cli.main(arguments)
        self.assertEqual(exit_code, 0, error.getvalue())
        self.assertEqual(error.getvalue(), "")
        return json.loads(output.getvalue())

    def test_all_eight_operation_pairs_and_overlapping_queue_facts(self) -> None:
        event_id = "parity % event"
        input_directory = paths.event_input_dir(event_id)
        input_directory.mkdir(parents=True)
        input_directory.joinpath("event.xml").write_bytes(b"opaque")

        with (
            mock.patch.object(
                service_information,
                "read_readiness",
                return_value={"ready": True, "reason": None},
            ),
            mock.patch.object(
                service_information,
                "service_identity",
                return_value=_identity(),
            ),
            TestClient(main.app) as client,
        ):
            submission_response = client.post(
                "/events",
                files=[("event_id", (None, event_id))],
            )
            self.assertEqual(submission_response.status_code, 202)
            acknowledgement = submission_response.json()

            rest = {
                "health": client.get("/healthz").json(),
                "config": client.get("/config").json(),
                "configurations": client.get("/configurations").json(),
                "list": client.get("/events").json(),
                "queue": client.get("/queue").json(),
                "status": client.get(acknowledgement["status_url"]).json(),
                "products": client.get(acknowledgement["products_url"]).json(),
            }

            def get_json(service_url: str, endpoint: str) -> object:
                self.assertEqual(service_url, cli.DEFAULT_SERVICE_URL)
                response = client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                return response.json()

            def post_json(
                service_url: str,
                endpoint: str,
                body: object,
                boundary: str,
            ) -> object:
                self.assertEqual(service_url, cli.DEFAULT_SERVICE_URL)
                self.assertEqual(endpoint, "/events")
                self.assertFalse(isinstance(body, (bytes, bytearray, memoryview)))
                self.assertTrue(boundary.startswith("shakemap-"))
                return acknowledgement

            with (
                mock.patch.object(cli, "_get_json", side_effect=get_json),
                mock.patch.object(cli, "_post_json", side_effect=post_json),
            ):
                self.assertEqual(self._run_cli(["health"]), rest["health"])
                self.assertEqual(self._run_cli(["config"]), rest["config"])
                self.assertEqual(
                    self._run_cli(["configurations"]),
                    rest["configurations"],
                )
                self.assertEqual(self._run_cli(["list"]), rest["list"])
                self.assertEqual(self._run_cli(["queue"]), rest["queue"])
                self.assertEqual(
                    self._run_cli(["status", event_id]),
                    rest["status"],
                )
                self.assertEqual(
                    self._run_cli(["products", event_id]),
                    rest["products"],
                )
                self.assertEqual(
                    self._run_cli(["submit", event_id]),
                    acknowledgement,
                )

        sequence = acknowledgement["internal_sequence"]
        event_row = rest["list"]["jobs"][0]
        queue_row = rest["queue"]["jobs"][0]
        detail_row = rest["status"]["jobs"][0]
        for row in (event_row, queue_row, detail_row):
            self.assertEqual(row["internal_sequence"], sequence)
            self.assertEqual(row["status"], "QUEUED")
        self.assertEqual(event_row["job_completed"], acknowledgement["job_completed"])
        self.assertEqual(event_row["products_ready"], acknowledgement["products_ready"])
        self.assertEqual(event_row["queue_position"], queue_row["queue_position"])
        self.assertEqual(event_row["waiting_reason"], queue_row["waiting_reason"])
        self.assertEqual(detail_row["queue_position"], queue_row["queue_position"])
        self.assertEqual(detail_row["waiting_reason"], queue_row["waiting_reason"])
        self.assertEqual(detail_row["configuration"]["selected"], "global")
        self.assertTrue(detail_row["overwrite"])
        self.assertEqual(
            event_row["shared_products_path"],
            acknowledgement["shared_products_path"],
        )
        self.assertIsNone(rest["products"]["current"])

    def test_openapi_contains_only_the_approved_public_paths(self) -> None:
        schema = main.app.openapi()
        expected = {
            "/healthz": {"get"},
            "/config": {"get"},
            "/configurations": {"get"},
            "/events": {"get", "post"},
            "/events/{event_id}": {"get"},
            "/events/{event_id}/products": {"get"},
            "/queue": {"get"},
        }
        self.assertEqual(
            {path: set(methods) for path, methods in schema["paths"].items()},
            expected,
        )
        for obsolete in (
            "/config/profiles",
            "/events/submit",
            "/list",
            "/submit",
            "/status/{event_id}",
        ):
            self.assertNotIn(obsolete, schema["paths"])

        request_body = schema["paths"]["/events"]["post"]["requestBody"]
        multipart = request_body["content"]["multipart/form-data"]["schema"]
        self.assertEqual(multipart["required"], ["event_id"])
        self.assertEqual(
            set(multipart["properties"]),
            {"event_id", "configuration", "overwrite", "files"},
        )
        self.assertEqual(multipart["properties"]["configuration"]["default"], "global")
        self.assertEqual(multipart["properties"]["overwrite"]["default"], "true")
        self.assertEqual(
            multipart["properties"]["overwrite"]["enum"],
            ["true", "false"],
        )
        self.assertEqual(multipart["properties"]["files"]["type"], "array")
        self.assertEqual(
            multipart["properties"]["files"]["items"]["format"],
            "binary",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
