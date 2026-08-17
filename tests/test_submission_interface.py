#!/usr/bin/env python3
"""Host tests for multipart submission through REST and the public CLI."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient

from shakemap_service import cli, main, paths, service_information, status, submission
from shakemap_service.config import Settings


READY = {"ready": True, "reason": None}


class SubmissionRestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_paths_settings = paths.settings
        self.previous_status_settings = status.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )
        paths.settings = configured
        status.settings = configured

    def tearDown(self) -> None:
        status.settings = self.previous_status_settings
        paths.settings = self.previous_paths_settings
        self.temporary.cleanup()

    def _input_directory(self, event_id: str) -> Path:
        directory = paths.event_input_dir(event_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _post(self, parts: list[tuple[str, tuple[object, ...]]]):
        with (
            mock.patch.object(
                service_information,
                "read_readiness",
                return_value=READY,
            ),
            TestClient(main.app, raise_server_exceptions=False) as client,
        ):
            return client.post("/events", files=parts)

    def test_not_ready_is_returned_before_body_parsing_or_acceptance(self) -> None:
        reason = "deployment readiness has not been recorded"
        with (
            mock.patch.object(
                service_information,
                "read_readiness",
                return_value={"ready": False, "reason": reason},
            ),
            mock.patch.object(
                main.Request,
                "form",
                side_effect=AssertionError("multipart was parsed"),
            ) as parsed,
            mock.patch.object(
                submission,
                "accept_request",
                side_effect=AssertionError("request was accepted"),
            ) as accepted,
            TestClient(main.app) as client,
        ):
            response = client.post(
                "/events",
                content=b"malformed body that must remain unread",
                headers={"content-type": "multipart/form-data; boundary=missing"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                "error": "service_unavailable",
                "message": reason,
                "details": [],
            },
        )
        parsed.assert_not_called()
        accepted.assert_not_called()
        self.assertFalse(paths.inputs_dir().exists())
        self.assertFalse(paths.queue_dir().exists())

    def test_strict_multipart_field_types_multiplicity_and_values(self) -> None:
        cases = (
            ([("configuration", (None, "global"))], "event_id"),
            (
                [
                    ("event_id", (None, "one")),
                    ("event_id", (None, "two")),
                ],
                "event_id",
            ),
            (
                [
                    ("event_id", (None, "evt")),
                    ("configuration", (None, "global")),
                    ("configuration", (None, "other")),
                ],
                "configuration",
            ),
            (
                [
                    ("event_id", (None, "evt")),
                    ("overwrite", (None, "true")),
                    ("overwrite", (None, "false")),
                ],
                "overwrite",
            ),
            (
                [("event_id", ("event.txt", b"evt", "text/plain"))],
                "event_id",
            ),
            (
                [
                    ("event_id", (None, "evt")),
                    ("configuration", ("configuration.txt", b"global")),
                ],
                "configuration",
            ),
            (
                [
                    ("event_id", (None, "evt")),
                    ("overwrite", ("overwrite.txt", b"true")),
                ],
                "overwrite",
            ),
            (
                [
                    ("event_id", (None, "evt")),
                    ("files", (None, "not a file")),
                ],
                "files",
            ),
            (
                [
                    ("event_id", (None, "evt")),
                    ("unexpected", (None, "value")),
                ],
                "unexpected",
            ),
        )
        for parts, expected in cases:
            with self.subTest(parts=parts):
                response = self._post(list(parts))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "request_rejected")
                self.assertIn(expected, response.json()["message"])
                self.assertNotEqual(response.status_code, 422)

        for value in ("", "1", "yes", "TRUE", "False"):
            with self.subTest(overwrite=value):
                response = self._post(
                    [
                        ("event_id", (None, "evt")),
                        ("overwrite", (None, value)),
                    ]
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("overwrite", response.json()["message"])

    def test_non_multipart_and_malformed_multipart_are_json_400(self) -> None:
        with (
            mock.patch.object(
                service_information,
                "read_readiness",
                return_value=READY,
            ),
            TestClient(main.app, raise_server_exceptions=False) as client,
        ):
            non_multipart = client.post("/events", json={"event_id": "evt"})
            malformed = client.post(
                "/events",
                content=b"not a multipart body",
                headers={"content-type": "multipart/form-data; boundary=broken"},
            )
        for response in (non_multipart, malformed):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                set(response.json()),
                {"error", "message", "details"},
            )
            self.assertNotEqual(response.status_code, 422)

    def test_multipart_spooling_oserror_is_pre_acceptance_json_400(self) -> None:
        async def fail_spooling(*args: object, **kwargs: object) -> object:
            raise OSError(f"spool failure at {self.temporary.name}")

        with (
            mock.patch.object(
                service_information,
                "read_readiness",
                return_value=READY,
            ),
            mock.patch.object(main.Request, "form", new=fail_spooling),
            mock.patch.object(
                submission,
                "accept_request",
                side_effect=AssertionError("request was accepted"),
            ) as accepted,
            TestClient(main.app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/events",
                content=b"caller bytes",
                headers={"content-type": "multipart/form-data; boundary=fixture"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                "error": "request_rejected",
                "message": "Caller uploads could not be received safely",
                "details": [],
            },
        )
        self.assertNotEqual(response.status_code, 422)
        self.assertNotIn(self.temporary.name, response.text)
        accepted.assert_not_called()
        self.assertFalse(paths.queue_dir().exists())

    def test_unexpected_multipart_parser_failure_remains_json_500(self) -> None:
        async def fail_parser(*args: object, **kwargs: object) -> object:
            raise RuntimeError(f"unexpected parser failure at {self.temporary.name}")

        with (
            mock.patch.object(
                service_information,
                "read_readiness",
                return_value=READY,
            ),
            mock.patch.object(main.Request, "form", new=fail_parser),
            mock.patch.object(
                submission,
                "accept_request",
                side_effect=AssertionError("request was accepted"),
            ) as accepted,
            TestClient(main.app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/events",
                content=b"caller bytes",
                headers={"content-type": "multipart/form-data; boundary=fixture"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "service_failure")
        self.assertNotIn(self.temporary.name, response.text)
        accepted.assert_not_called()

    def test_cleanup_failure_does_not_reject_durably_accepted_work(self) -> None:
        private_path = self.temporary.name

        class CleanupFailingForm:
            @staticmethod
            def multi_items() -> list[tuple[str, object]]:
                return [("event_id", "accepted")]

            async def close(self) -> None:
                raise OSError(f"cleanup failure at {private_path}")

        async def parsed_form(*args: object, **kwargs: object) -> object:
            return CleanupFailingForm()

        accepted_result = submission.SubmissionResult(
            event_id="accepted",
            internal_sequence=9,
            status="QUEUED",
            warnings=(),
            requested_configuration="global",
            overwrite=True,
            status_path="/private/status.json",
            shared_input_path="/operator/runtime/shakemap/data/inputs/accepted",
            shared_products_path=(
                "/operator/runtime/shakemap/products/accepted/current/products"
            ),
        )
        with (
            mock.patch.object(
                service_information,
                "read_readiness",
                return_value=READY,
            ),
            mock.patch.object(main.Request, "form", new=parsed_form),
            mock.patch.object(
                submission,
                "accept_request",
                return_value=accepted_result,
            ) as accepted,
            TestClient(main.app, raise_server_exceptions=False) as client,
        ):
            response = client.post(
                "/events",
                content=b"caller bytes",
                headers={"content-type": "multipart/form-data; boundary=fixture"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["internal_sequence"], 9)
        accepted.assert_called_once()

    def test_manual_input_defaults_and_exact_acknowledgement(self) -> None:
        event_id = "σεισμός % 1"
        self._input_directory(event_id).joinpath("event.xml").write_bytes(b"opaque")

        response = self._post([("event_id", (None, event_id))])

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json(),
            {
                "event_id": event_id,
                "internal_sequence": 1,
                "status": "QUEUED",
                "job_completed": False,
                "products_ready": False,
                "status_url": (
                    "/events/%CF%83%CE%B5%CE%B9%CF%83%CE%BC%CF%8C%CF%82%20%25%201"
                ),
                "products_url": (
                    "/events/%CF%83%CE%B5%CE%B9%CF%83%CE%BC%CF%8C%CF%82%20%25%201/products"
                ),
                "shared_input_path": (
                    "/operator/runtime/shakemap/data/inputs/σεισμός % 1"
                ),
                "shared_products_path": (
                    "/operator/runtime/shakemap/products/σεισμός % 1/current/products"
                ),
                "requested_configuration": "global",
                "overwrite": True,
                "warnings": [],
            },
        )
        queued = status.read_status(1)
        self.assertEqual(queued.event_id, event_id)
        self.assertEqual(queued.status, "QUEUED")
        self.assertTrue(queued.overwrite)

    def test_missing_event_file_and_repeated_event_id_submission(self) -> None:
        missing_directory = self._input_directory("missing")
        missing_directory.joinpath("other.bin").write_bytes(b"opaque")
        missing = self._post([("event_id", (None, "missing"))])
        self.assertEqual(missing.status_code, 400)
        self.assertIn("event.xml", missing.json()["message"])
        self.assertFalse(paths.queue_entry_dir(1).exists())

        accepted_directory = self._input_directory("repeat")
        accepted_directory.joinpath("event.xml").write_bytes(b"opaque")
        first = self._post([("event_id", (None, "repeat"))])
        second = self._post([("event_id", (None, "repeat"))])
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(
            (first.json()["internal_sequence"], second.json()["internal_sequence"]),
            (1, 2),
        )

    def test_mixed_repeated_uploads_stream_bytes_and_report_replacement(self) -> None:
        directory = self._input_directory("evt")
        directory.joinpath("event.xml").write_bytes(b"preceding")
        directory.joinpath("manual.bin").write_bytes(b"manual")
        event_bytes = bytes(range(256)) * 600
        extra_bytes = b"z" * (submission.COPY_CHUNK_SIZE * 2 + 7)

        response = self._post(
            [
                ("event_id", (None, "evt")),
                ("configuration", (None, "regional set+1")),
                ("overwrite", (None, "false")),
                ("files", ("event.xml", event_bytes, "application/octet-stream")),
                ("files", ("odd name.数据", extra_bytes, "application/octet-stream")),
            ]
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["requested_configuration"], "regional set+1")
        self.assertFalse(payload["overwrite"])
        self.assertEqual(
            payload["warnings"],
            ["Uploaded 'event.xml' replaced the existing caller input file"],
        )
        self.assertEqual(directory.joinpath("event.xml").read_bytes(), event_bytes)
        self.assertEqual(directory.joinpath("odd name.数据").read_bytes(), extra_bytes)
        snapshot = paths.queue_request_dir(1)
        self.assertEqual(snapshot.joinpath("event.xml").read_bytes(), event_bytes)
        self.assertEqual(snapshot.joinpath("manual.bin").read_bytes(), b"manual")
        self.assertEqual(snapshot.joinpath("odd name.数据").read_bytes(), extra_bytes)
        self.assertEqual(status.read_status(1).warnings, payload["warnings"])

    def test_duplicate_and_unsafe_upload_basenames_are_rejected(self) -> None:
        cases = (
            [
                ("event_id", (None, "evt")),
                ("files", ("event.xml", b"one")),
                ("files", ("event.xml", b"two")),
            ],
            [
                ("event_id", (None, "evt")),
                ("files", ("../event.xml", b"one")),
            ],
            [
                ("event_id", (None, "evt")),
                ("files", ("", b"one")),
            ],
        )
        for parts in cases:
            with self.subTest(parts=parts):
                response = self._post(parts)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "request_rejected")
                self.assertFalse(paths.queue_entry_dir(1).exists())

    def test_adapter_passes_file_objects_without_reading_them(self) -> None:
        observed: list[tuple[str, int]] = []

        def accept(
            event_id: str,
            uploads: list[submission.Upload],
            *,
            configuration: str,
            overwrite: bool,
            _require_ready: bool,
            _readiness_reader: object,
        ) -> submission.SubmissionResult:
            for upload in uploads:
                observed.append((upload.basename, upload.stream.tell()))
            return submission.SubmissionResult(
                event_id=event_id,
                internal_sequence=9,
                status="QUEUED",
                warnings=(),
                requested_configuration=configuration,
                overwrite=overwrite,
                status_path="unused",
                shared_input_path="/operator/runtime/shakemap/data/inputs/evt",
                shared_products_path=(
                    "/operator/runtime/shakemap/products/evt/current/products"
                ),
            )

        with (
            mock.patch.object(
                service_information,
                "read_readiness",
                return_value=READY,
            ),
            mock.patch.object(submission, "accept_request", side_effect=accept),
            TestClient(main.app) as client,
        ):
            response = client.post(
                "/events",
                files=[
                    ("event_id", (None, "evt")),
                    ("files", ("event.xml", b"opaque")),
                    ("files", ("extra.bin", b"extra")),
                ],
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(observed, [("event.xml", 0), ("extra.bin", 0)])

    def test_typed_input_failure_is_400_and_acceptance_ambiguity_is_500(self) -> None:
        self._input_directory("snapshot").joinpath("event.xml").write_bytes(b"event")
        with mock.patch.object(
            submission,
            "_copy_regular_file",
            side_effect=OSError("private snapshot path detail"),
        ):
            rejected = self._post([("event_id", (None, "snapshot"))])
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["error"], "request_rejected")
        self.assertNotIn(self.temporary.name, rejected.text)
        self.assertFalse(paths.queue_entry_dir(1).exists())

        self._input_directory("ambiguous").joinpath("event.xml").write_bytes(b"event")
        with mock.patch.object(
            submission,
            "_sync_published_queue_parent",
            side_effect=OSError("private queue path detail"),
        ):
            ambiguous = self._post([("event_id", (None, "ambiguous"))])
        self.assertEqual(ambiguous.status_code, 500)
        self.assertEqual(
            ambiguous.json(),
            {
                "error": "service_failure",
                "message": "Submission could not be completed safely",
                "details": [],
            },
        )
        self.assertNotIn(self.temporary.name, ambiguous.text)
        self.assertEqual(status.read_status(1).event_id, "ambiguous")

    def test_upload_destination_inspection_failure_is_400_before_acceptance(
        self,
    ) -> None:
        input_directory = self._input_directory("inspect")
        input_directory.joinpath("event.xml").write_bytes(b"preceding")
        with mock.patch.object(
            submission,
            "_existing_regular_file",
            side_effect=OSError("private input path detail"),
        ):
            response = self._post(
                [
                    ("event_id", (None, "inspect")),
                    ("files", ("event.xml", b"replacement")),
                ]
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                "error": "request_rejected",
                "message": "caller upload destinations could not be inspected safely",
                "details": [],
            },
        )
        self.assertNotIn("private input path detail", response.text)
        self.assertEqual(
            input_directory.joinpath("event.xml").read_bytes(),
            b"preceding",
        )
        self.assertFalse(paths.queue_dir().exists())

    def test_unsafe_canonical_input_directory_is_400_before_acceptance(self) -> None:
        caller_directory = Path(self.temporary.name) / "caller-owned"
        caller_directory.mkdir()
        caller_directory.joinpath("event.xml").write_bytes(b"preserve")
        paths.inputs_dir().mkdir(parents=True)
        paths.event_input_dir("unsafe").symlink_to(
            caller_directory,
            target_is_directory=True,
        )

        response = self._post([("event_id", (None, "unsafe"))])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                "error": "request_rejected",
                "message": "canonical event input directory could not be opened safely",
                "details": [],
            },
        )
        self.assertEqual(
            caller_directory.joinpath("event.xml").read_bytes(),
            b"preserve",
        )
        self.assertTrue(paths.event_input_dir("unsafe").is_symlink())
        self.assertFalse(paths.queue_dir().exists())

    def test_malformed_durable_sequence_state_is_sanitized_500(self) -> None:
        self._input_directory("sequence").joinpath("event.xml").write_bytes(b"event")
        queue_root = paths.queue_dir()
        queue_root.mkdir(parents=True)
        paths.queue_sequence_file().write_text("malformed\n", encoding="ascii")

        response = self._post([("event_id", (None, "sequence"))])

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {
                "error": "service_failure",
                "message": "Submission could not be completed safely",
                "details": [],
            },
        )
        self.assertNotIn("malformed queue sequence state", response.text)
        self.assertNotIn(self.temporary.name, response.text)
        self.assertEqual(
            [entry.name for entry in queue_root.iterdir()],
            [".next-sequence"],
        )

    def test_known_publication_error_is_400_and_unexpected_error_is_500(self) -> None:
        parts = [("event_id", (None, "evt"))]
        with mock.patch.object(
            submission,
            "accept_request",
            side_effect=submission.InputPublicationError("private/path"),
        ):
            publication = self._post(parts)
        self.assertEqual(publication.status_code, 400)
        self.assertEqual(publication.json()["error"], "request_rejected")
        self.assertNotIn("private/path", publication.text)

        with mock.patch.object(
            submission,
            "accept_request",
            side_effect=RuntimeError("private/path"),
        ):
            unexpected = self._post(parts)
        self.assertEqual(unexpected.status_code, 500)
        self.assertEqual(unexpected.json()["error"], "service_failure")
        self.assertNotIn("private/path", unexpected.text)


class GuardedBytesIO(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        if size < 1 or size > cli.MULTIPART_CHUNK_SIZE:
            raise AssertionError(f"unbounded multipart file read: {size}")
        self.read_sizes.append(size)
        return super().read(size)


class SubmissionCliTests(unittest.TestCase):
    def test_post_transport_keeps_the_request_body_iterable(self) -> None:
        body = iter((b"first", b"second"))
        observed: dict[str, object] = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"accepted":true}'

        def open_request(request: object, *, timeout: int):
            observed["method"] = request.get_method()
            observed["data"] = request.data
            observed["content_type"] = request.get_header("Content-type")
            observed["content_length"] = request.get_header("Content-length")
            observed["accept"] = request.get_header("Accept")
            observed["timeout"] = timeout
            return Response()

        with mock.patch.object(cli, "urlopen", side_effect=open_request):
            payload = cli._post_json(
                "http://service.test/base/",
                "/events",
                body,
                "boundary",
            )

        self.assertEqual(payload, {"accepted": True})
        self.assertEqual(observed["method"], "POST")
        self.assertIs(observed["data"], body)
        self.assertEqual(
            observed["content_type"],
            "multipart/form-data; boundary=boundary",
        )
        self.assertIsNone(observed["content_length"])
        self.assertEqual(observed["accept"], "application/json")
        self.assertEqual(observed["timeout"], 30)

    def test_submit_streams_exact_fields_and_repeated_files(self) -> None:
        event_stream = GuardedBytesIO(b"event-bytes")
        extra_stream = GuardedBytesIO(b"extra-bytes")
        calls: list[tuple[str, str, str]] = []

        def open_files(
            source_paths: list[str],
            resources: contextlib.ExitStack,
        ) -> list[tuple[str, io.BytesIO]]:
            self.assertEqual(source_paths, ["/inputs/event.xml", "/inputs/odd name.数据"])
            resources.callback(event_stream.close)
            resources.callback(extra_stream.close)
            return [("event.xml", event_stream), ("odd name.数据", extra_stream)]

        def post(
            service_url: str,
            endpoint: str,
            body: object,
            boundary: str,
        ) -> object:
            calls.append((service_url, endpoint, boundary))
            self.assertNotIsInstance(body, (bytes, bytearray, memoryview, list, tuple))
            expected = {
                b"event_id",
                b"evt",
                b"configuration",
                b"regional set+1",
                b"overwrite",
                b"false",
                b"event-bytes",
                b"extra-bytes",
                "odd name.数据".encode("utf-8"),
            }
            seen: set[bytes] = set()
            for chunk in body:
                seen.update(fragment for fragment in expected if fragment in chunk)
            self.assertEqual(seen, expected)
            return {"status": "QUEUED", "internal_sequence": 4}

        output = io.StringIO()
        error = io.StringIO()
        with (
            mock.patch.object(cli, "_open_submission_files", side_effect=open_files),
            mock.patch.object(cli, "_post_json", side_effect=post),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            exit_code = cli.main(
                [
                    "--url",
                    "http://service.test/base/",
                    "submit",
                    "evt",
                    "--configuration",
                    "regional set+1",
                    "--overwrite",
                    "false",
                    "--file",
                    "/inputs/event.xml",
                    "--file",
                    "/inputs/odd name.数据",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(
            json.loads(output.getvalue()),
            {"status": "QUEUED", "internal_sequence": 4},
        )
        self.assertEqual(calls[0][:2], ("http://service.test/base/", "/events"))
        self.assertTrue(event_stream.read_sizes)
        self.assertTrue(extra_stream.read_sizes)
        self.assertTrue(event_stream.closed)
        self.assertTrue(extra_stream.closed)

    def test_submit_defaults_and_resource_cleanup_on_transport_failure(self) -> None:
        stream = GuardedBytesIO(b"event")
        observed_fields: list[tuple[str, str]] = []

        def open_files(
            source_paths: list[str],
            resources: contextlib.ExitStack,
        ) -> list[tuple[str, io.BytesIO]]:
            resources.callback(stream.close)
            return [("event.xml", stream)]

        def fail(
            service_url: str,
            endpoint: str,
            body: object,
            boundary: str,
        ) -> object:
            iterator = iter(body)
            for chunk in iterator:
                if chunk in {b"evt", b"global", b"true"}:
                    observed_fields.append((chunk.decode("ascii"), boundary))
                if chunk == b"event":
                    break
            raise URLError("offline")

        output = io.StringIO()
        error = io.StringIO()
        with (
            mock.patch.object(cli, "_open_submission_files", side_effect=open_files),
            mock.patch.object(cli, "_post_json", side_effect=fail),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            exit_code = cli.main(["submit", "evt", "--file", "event.xml"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("offline", error.getvalue())
        self.assertEqual([value for value, _ in observed_fields], ["evt", "global", "true"])
        self.assertTrue(stream.closed)

    def test_local_file_validation_and_http_errors_use_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "one" / "event.xml"
            second = Path(temporary) / "two" / "event.xml"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"one")
            second.write_bytes(b"two")

            output = io.StringIO()
            error = io.StringIO()
            with (
                mock.patch.object(
                    cli,
                    "_post_json",
                    side_effect=AssertionError("transport should not run"),
                ) as post,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                duplicate_exit = cli.main(
                    [
                        "submit",
                        "evt",
                        "--file",
                        str(first),
                        "--file",
                        str(second),
                    ]
                )
            self.assertEqual(duplicate_exit, 1)
            self.assertIn("duplicate upload basenames", error.getvalue())
            self.assertEqual(output.getvalue(), "")
            post.assert_not_called()

        http_error = HTTPError(
            "http://localhost:9010/events",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"request_rejected"}'),
        )
        output = io.StringIO()
        error = io.StringIO()
        with (
            mock.patch.object(cli, "_open_submission_files", return_value=[]),
            mock.patch.object(cli, "_post_json", side_effect=http_error),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            http_exit = cli.main(["submit", "evt"])
        self.assertEqual(http_exit, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("HTTP 400 Bad Request", error.getvalue())
        self.assertIn("request_rejected", error.getvalue())

    def test_real_opened_files_are_regular_and_closed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "event.xml"
            source.write_bytes(b"event")
            captured: list[io.BufferedReader] = []
            real_open_files = cli._open_submission_files

            def tracked_open_files(
                source_paths: list[str],
                resources: contextlib.ExitStack,
            ) -> list[tuple[str, io.BufferedReader]]:
                opened = real_open_files(source_paths, resources)
                captured.extend(stream for _, stream in opened)
                return opened

            def consume(
                service_url: str,
                endpoint: str,
                body: object,
                boundary: str,
            ) -> object:
                for _ in body:
                    pass
                return {"accepted": True}

            with (
                mock.patch.object(
                    cli,
                    "_open_submission_files",
                    side_effect=tracked_open_files,
                ),
                mock.patch.object(cli, "_post_json", side_effect=consume),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["submit", "evt", "--file", str(source)]), 0)

            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0].closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
