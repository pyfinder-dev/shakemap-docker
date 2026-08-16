#!/usr/bin/env python3
"""Host tests for public event-collection and queue projections."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest import mock

from fastapi.testclient import TestClient

from shakemap_service import cli, main, paths, public_views, status
from shakemap_service.config import Settings
from shakemap_service.status import CalculationRecord, LifecycleState
from shakemap_service.submission import Upload, accept_request


def _record(
    sequence: int,
    event_id: str,
    lifecycle: LifecycleState,
    *,
    phase: str | None = None,
) -> CalculationRecord:
    if phase is None and lifecycle != LifecycleState.QUEUED:
        phase = "preceding_tree_disposition"
    started_at = None if lifecycle == LifecycleState.QUEUED else "2026-08-16T10:01:00Z"
    completed_at = (
        "2026-08-16T10:02:00Z"
        if lifecycle in {LifecycleState.SUCCESS, LifecycleState.FAILED}
        else None
    )
    return CalculationRecord(
        schema_version=1,
        event_id=event_id,
        internal_sequence=sequence,
        status=lifecycle.value,
        overwrite=True,
        warnings=[],
        request={
            "snapshot": "request",
            "manifest": "request-manifest.json",
            "input_mode": "upload",
        },
        configuration={"selected": "global"},
        progress={
            "phase": phase,
            "phase_started_at": started_at if phase is not None else None,
            "module_plan": [
                "select",
                "assemble",
                "model",
                "contour",
                "mapping",
                "stations",
                "gridxml",
            ],
            "current_module": None,
            "completed_modules": [],
        },
        native_outcome=None,
        service_outcome=None,
        failure=(
            {
                "phase": phase,
                "code": "fixture",
                "message": "fixture failure",
            }
            if lifecycle == LifecycleState.FAILED
            else None
        ),
        timestamps={
            "submitted_at": f"2026-08-16T10:00:{sequence:02d}Z",
            "started_at": started_at,
            "native_started_at": None,
            "native_finished_at": None,
            "completed_at": completed_at,
        },
        shared_paths={
            "input": f"/operator/runtime/shakemap/data/inputs/{event_id}",
            "products": (
                f"/operator/runtime/shakemap/products/{event_id}/current/products"
            ),
            "provenance": None,
            "product_manifest": None,
            "service_log": None,
            "shake_log": None,
        },
    )


class OperationalProjectionTests(unittest.TestCase):
    def _views(
        self,
        *,
        queue: list[CalculationRecord] | None = None,
        current: list[CalculationRecord] | None = None,
        maximum_running: int = 10,
    ) -> public_views.OperationalViews:
        with (
            mock.patch.object(
                public_views,
                "scan_queue_records",
                return_value=(queue or [], []),
            ),
            mock.patch.object(
                public_views,
                "scan_current_records",
                return_value=(current or [], []),
            ),
        ):
            return public_views.build_operational_views(
                service_settings=Settings(max_concurrent=maximum_running)
            )

    def test_empty_state(self) -> None:
        views = self._views()
        self.assertEqual(views.events, {"jobs": []})
        self.assertEqual(
            views.queue,
            {
                "capacity": {
                    "maximum_running": 10,
                    "running": 0,
                    "available": 10,
                    "queued": 0,
                },
                "jobs": [],
            },
        )

    def test_mixed_rows_preserve_repeated_ids_and_sequence_order(self) -> None:
        current = [
            _record(4, "repeat", LifecycleState.RUNNING, phase="native_execution"),
            _record(1, "finished", LifecycleState.FAILED, phase="record_finalization"),
        ]
        queue = [
            _record(7, "repeat", LifecycleState.QUEUED),
            _record(6, "other", LifecycleState.QUEUED),
        ]

        views = self._views(queue=queue, current=current, maximum_running=2)

        rows = views.events["jobs"]
        self.assertEqual([row["internal_sequence"] for row in rows], [1, 4, 6, 7])
        self.assertEqual([row["event_id"] for row in rows], ["finished", "repeat", "other", "repeat"])
        self.assertTrue(rows[0]["job_completed"])
        self.assertFalse(rows[0]["products_ready"])
        self.assertEqual(rows[1]["phase"], "native_execution")
        self.assertEqual(rows[2]["queue_position"], 1)
        self.assertEqual(rows[2]["waiting_reason"], "awaiting_scheduler")
        self.assertEqual(rows[3]["queue_position"], 2)
        self.assertEqual(rows[3]["waiting_reason"], "same_event_active")
        self.assertEqual(views.queue["jobs"], [
            {
                "event_id": row["event_id"],
                "internal_sequence": row["internal_sequence"],
                "status": row["status"],
                "queue_position": row["queue_position"],
                "waiting_reason": row["waiting_reason"],
                "submitted_at": row["submitted_at"],
            }
            for row in rows[2:]
        ])

    def test_running_records_in_either_location_are_counted_once_by_sequence(self) -> None:
        first = _record(1, "queue-running", LifecycleState.RUNNING)
        second = _record(2, "current-running", LifecycleState.RUNNING)
        completed_second = _record(2, "current-running", LifecycleState.SUCCESS)
        queued = _record(3, "waiting", LifecycleState.QUEUED)

        views = self._views(
            queue=[first, second, queued],
            current=[completed_second],
            maximum_running=4,
        )

        self.assertEqual(
            [row["internal_sequence"] for row in views.events["jobs"]],
            [1, 2, 3],
        )
        self.assertEqual(
            views.queue["capacity"],
            {
                "maximum_running": 4,
                "running": 2,
                "available": 2,
                "queued": 1,
            },
        )
        self.assertEqual(views.events["jobs"][1]["status"], "SUCCESS")

    def test_waiting_reason_precedence_and_capacity_math(self) -> None:
        running = _record(1, "same", LifecycleState.RUNNING)
        same = _record(2, "same", LifecycleState.QUEUED)
        different = _record(3, "different", LifecycleState.QUEUED)

        exhausted = self._views(
            queue=[same, different],
            current=[running],
            maximum_running=1,
        ).queue
        self.assertEqual(exhausted["capacity"]["available"], 0)
        self.assertEqual(
            [row["waiting_reason"] for row in exhausted["jobs"]],
            ["same_event_active", "worker_capacity"],
        )

        available = self._views(
            queue=[different],
            current=[running],
            maximum_running=2,
        ).queue
        self.assertEqual(available["capacity"]["available"], 1)
        self.assertEqual(available["jobs"][0]["waiting_reason"], "awaiting_scheduler")

    def test_success_flags_come_only_from_lifecycle_state(self) -> None:
        success = _record(1, "success", LifecycleState.SUCCESS)
        success.shared_paths["products"] = "/path/that/need/not/exist"
        row = self._views(current=[success]).events["jobs"][0]
        self.assertTrue(row["job_completed"])
        self.assertTrue(row["products_ready"])

    def test_terminal_record_retained_in_queue_storage_remains_in_events(self) -> None:
        failed = _record(1, "promotion-failed", LifecycleState.FAILED)
        views = self._views(queue=[failed])
        self.assertEqual(
            [row["internal_sequence"] for row in views.events["jobs"]],
            [1],
        )
        self.assertEqual(views.queue["capacity"]["queued"], 0)
        self.assertEqual(views.queue["jobs"], [])

    def test_conflicting_duplicate_sequence_identity_fails_closed(self) -> None:
        with self.assertRaises(public_views.DurableStateError):
            self._views(
                queue=[_record(1, "one", LifecycleState.RUNNING)],
                current=[_record(1, "two", LifecycleState.RUNNING)],
            )


class RestInterfaceTests(unittest.TestCase):
    def test_malformed_durable_record_is_json_http_500(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Settings(runtime_root=temporary)
            queue = Path(temporary) / "shakemap/.service/queue/00000000000000000001"
            queue.mkdir(parents=True)
            (queue / "status.json").write_text("{broken", encoding="utf-8")
            previous = paths.settings
            paths.settings = configured
            try:
                with TestClient(main.app, raise_server_exceptions=False) as client:
                    for endpoint in ("/events", "/queue"):
                        with self.subTest(endpoint=endpoint):
                            response = client.get(endpoint)
                            self.assertEqual(response.status_code, 500)
                            body = response.json()
                            self.assertEqual(body["error"], "service_failure")
                            self.assertEqual(body["message"], "Durable calculation state is malformed")
                            self.assertEqual(body["details"][0]["source"], "queue")
                            self.assertEqual(
                                body["details"][0]["entry"],
                                "00000000000000000001",
                            )
                            self.assertNotIn(temporary, response.text)
            finally:
                paths.settings = previous

    def test_timestamp_and_phase_records_are_validated_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Settings(
                runtime_root=temporary,
                shared_runtime_root="/operator/runtime",
            )
            previous_paths = paths.settings
            previous_status = status.settings
            paths.settings = configured
            status.settings = configured
            try:
                whole = accept_request(
                    "whole",
                    [Upload("event.xml", io.BytesIO(b"whole"))],
                )
                fractional = accept_request(
                    "fractional",
                    [Upload("event.xml", io.BytesIO(b"fractional"))],
                )
                for accepted, value in (
                    (whole, "2026-08-16T10:20:00Z"),
                    (fractional, "2026-08-16T10:20:00.123456789Z"),
                ):
                    target = paths.queue_status_file(accepted.internal_sequence)
                    payload = json.loads(target.read_text(encoding="utf-8"))
                    payload["timestamps"]["submitted_at"] = value
                    target.write_text(json.dumps(payload), encoding="utf-8")

                with TestClient(main.app) as client:
                    for endpoint in ("/events", "/queue"):
                        response = client.get(endpoint)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(
                            [row["submitted_at"] for row in response.json()["jobs"]],
                            [
                                "2026-08-16T10:20:00Z",
                                "2026-08-16T10:20:00.123456789Z",
                            ],
                        )

                malformed = accept_request(
                    "malformed",
                    [Upload("event.xml", io.BytesIO(b"malformed"))],
                )
                status.transition_to_running(malformed.internal_sequence)
                target = paths.queue_status_file(malformed.internal_sequence)
                payload = json.loads(target.read_text(encoding="utf-8"))
                payload["progress"]["phase"] = "native_module_guess"
                payload["progress"]["phase_started_at"] = "2026-08-16T10:20Z"
                target.write_text(json.dumps(payload), encoding="utf-8")

                with TestClient(main.app, raise_server_exceptions=False) as client:
                    for endpoint in ("/events", "/queue"):
                        response = client.get(endpoint)
                        self.assertEqual(response.status_code, 500)
                        self.assertEqual(response.json()["error"], "service_failure")
                        self.assertNotIn(temporary, response.text)
            finally:
                status.settings = previous_status
                paths.settings = previous_paths

    def test_rest_and_cli_return_the_same_json(self) -> None:
        queue = [_record(2, "waiting", LifecycleState.QUEUED)]
        current = [_record(1, "running", LifecycleState.RUNNING)]
        with (
            mock.patch.object(public_views, "scan_queue_records", return_value=(queue, [])),
            mock.patch.object(public_views, "scan_current_records", return_value=(current, [])),
            mock.patch.object(public_views, "settings", Settings(max_concurrent=2)),
            TestClient(main.app) as client,
        ):
            rest = {
                "list": client.get("/events").json(),
                "queue": client.get("/queue").json(),
            }

            def fetch(service_url: str, endpoint: str) -> object:
                self.assertEqual(service_url, "http://service.test")
                return client.get(endpoint).json()

            for command in ("list", "queue"):
                with self.subTest(command=command):
                    output = io.StringIO()
                    error = io.StringIO()
                    with (
                        mock.patch.object(cli, "_get_json", side_effect=fetch),
                        contextlib.redirect_stdout(output),
                        contextlib.redirect_stderr(error),
                    ):
                        exit_code = cli.main(["--url", "http://service.test", command])
                    self.assertEqual(exit_code, 0)
                    self.assertEqual(json.loads(output.getvalue()), rest[command])
                    self.assertEqual(error.getvalue(), "")


class CliTransportTests(unittest.TestCase):
    class _Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def test_url_selection_and_json_only_stdout(self) -> None:
        response = self._Response(b'{"jobs":[]}')
        output = io.StringIO()
        error = io.StringIO()
        with (
            mock.patch.object(cli, "urlopen", return_value=response) as opened,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            exit_code = cli.main(["--url", "http://example.test/root/", "list"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), '{"jobs":[]}\n')
        self.assertEqual(error.getvalue(), "")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://example.test/root/events")
        self.assertEqual(opened.call_args.kwargs, {"timeout": 30})

    def test_transport_and_http_errors_use_only_stderr(self) -> None:
        http_error = HTTPError(
            "http://service/queue",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b'{"error":"service_failure"}'),
        )
        for error_value in (URLError("connection refused"), http_error):
            with self.subTest(error=type(error_value).__name__):
                output = io.StringIO()
                error = io.StringIO()
                with (
                    mock.patch.object(cli, "urlopen", side_effect=error_value),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(error),
                ):
                    exit_code = cli.main(["queue"])
                self.assertNotEqual(exit_code, 0)
                self.assertEqual(output.getvalue(), "")
                self.assertTrue(error.getvalue().startswith("ERROR:"))

    def test_client_has_no_runtime_or_native_access(self) -> None:
        source = Path(cli.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "from .paths",
            "from .status",
            "from .queue",
            "import docker",
            "docker run",
            "subprocess",
            "RUNTIME_ROOT",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
