from __future__ import annotations

import io
import importlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event
from unittest import mock

from shakemap_service import paths, status, submission
from shakemap_service.config import Settings
from shakemap_service.queue import discover_queue
from shakemap_service.submission import Upload, accept_request


class ChunkGuard(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        if size < 1 or size > submission.COPY_CHUNK_SIZE:
            raise AssertionError(f"upload requested an unbounded read size: {size}")
        self.read_sizes.append(size)
        return super().read(size)


class FailingStream(io.BytesIO):
    def __init__(self, first_chunk: bytes) -> None:
        super().__init__(first_chunk)
        self.calls = 0

    def read(self, size: int = -1) -> bytes:
        self.calls += 1
        if self.calls > 1:
            raise OSError("injected upload read failure")
        return super().read(min(size, 2))


class RequestAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_path_settings = paths.settings
        self.previous_status_settings = status.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )
        paths.settings = configured
        status.settings = configured

    def tearDown(self) -> None:
        paths.settings = self.previous_path_settings
        status.settings = self.previous_status_settings
        self.temporary.cleanup()

    def _input_directory(self, event_id: str = "evt") -> Path:
        directory = paths.event_input_dir(event_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _queue_entries(self) -> list[Path]:
        root = paths.queue_dir()
        if not root.exists():
            return []
        return sorted(entry for entry in root.iterdir() if entry.name.isdigit())

    def _private_queue_trees(self) -> list[Path]:
        root = paths.queue_dir()
        if not root.exists():
            return []
        return sorted(
            entry for entry in root.iterdir() if entry.name.startswith(".accept-")
        )

    def test_oversized_event_id_is_rejected_before_persistence(self) -> None:
        upload = io.BytesIO(b"event")

        with self.assertRaisesRegex(
            ValueError,
            "uses 232 UTF-8 bytes; shorten it to at most 231 bytes",
        ):
            accept_request(
                "a" * 232,
                [Upload("event.xml", upload)],
            )

        self.assertEqual(upload.tell(), 0)
        self.assertEqual(list(paths.runtime_root().iterdir()), [])

    def test_zero_upload_snapshot_uses_safe_regular_files(self) -> None:
        input_directory = self._input_directory("σεισμός 01")
        (input_directory / "event.xml").write_bytes(b"opaque event bytes\x00")
        (input_directory / "custom.native+file").write_bytes(b"custom")
        (input_directory / "nested").mkdir()
        (input_directory / "nested" / "ignored.bin").write_bytes(b"nested")
        (input_directory / "linked.bin").symlink_to(
            input_directory / "custom.native+file"
        )

        result = accept_request("σεισμός 01")

        snapshot = paths.queue_request_dir(result.internal_sequence)
        self.assertEqual(
            sorted(path.name for path in snapshot.iterdir()),
            ["custom.native+file", "event.xml"],
        )
        self.assertEqual(
            (snapshot / "event.xml").read_bytes(),
            b"opaque event bytes\x00",
        )
        self.assertEqual(result.status, "QUEUED")
        self.assertEqual(
            status.read_status(result.internal_sequence).request["input_mode"],
            "directory",
        )

    def test_uploads_preserve_bytes_streaming_warning_and_snapshot(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"preceding event")
        (directory / "manual.bin").write_bytes(b"manual")
        event_payload = bytes(range(256)) * 800
        ancillary_payload = b"z" * (submission.COPY_CHUNK_SIZE * 2 + 17)
        event_stream = ChunkGuard(event_payload)
        ancillary_stream = ChunkGuard(ancillary_payload)
        original_read = os.read
        snapshot_reads: list[int] = []

        def bounded_read(descriptor: int, size: int) -> bytes:
            if size < 1 or size > submission.COPY_CHUNK_SIZE:
                raise AssertionError(f"snapshot requested an unbounded read: {size}")
            snapshot_reads.append(size)
            return original_read(descriptor, size)

        with mock.patch.object(submission.os, "read", side_effect=bounded_read):
            result = accept_request(
                "evt",
                [
                    Upload("event.xml", event_stream),
                    Upload("arbitrary.native", ancillary_stream),
                ],
                configuration="regional set+1",
                overwrite=False,
            )

        self.assertTrue(event_stream.read_sizes)
        self.assertTrue(ancillary_stream.read_sizes)
        self.assertTrue(snapshot_reads)
        self.assertEqual((directory / "event.xml").read_bytes(), event_payload)
        snapshot = paths.queue_request_dir(result.internal_sequence)
        self.assertEqual((snapshot / "event.xml").read_bytes(), event_payload)
        self.assertEqual(
            (snapshot / "arbitrary.native").read_bytes(), ancillary_payload
        )
        self.assertEqual((snapshot / "manual.bin").read_bytes(), b"manual")
        self.assertEqual(result.requested_configuration, "regional set+1")
        self.assertFalse(result.overwrite)
        self.assertEqual(
            result.warnings,
            (submission._replacement_warning("event.xml"),),
        )
        self.assertEqual(
            status.read_status(result.internal_sequence).warnings,
            list(result.warnings),
        )
        self.assertEqual(self._private_queue_trees(), [])

    def test_caller_names_resembling_private_files_are_included(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"event")
        payloads = {
            ".upload-" + "1" * 32 + ".tmp": b"old-stream-shape",
            ".upload-backup-" + "2" * 32 + ".tmp": b"old-backup-shape",
            "stream-" + "3" * 32 + ".tmp": b"stream-shape",
            "preceding-" + "4" * 32 + ".tmp": b"backup-shape",
        }

        result = accept_request(
            "evt",
            [Upload(name, io.BytesIO(payload)) for name, payload in payloads.items()],
        )

        manifest = json.loads(
            (paths.queue_entry_dir(result.internal_sequence) / "request-manifest.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["basename"] for item in manifest["files"]],
            sorted(["event.xml", *payloads]),
        )
        for name, payload in payloads.items():
            self.assertEqual((directory / name).read_bytes(), payload)
            self.assertEqual(
                (paths.queue_request_dir(result.internal_sequence) / name).read_bytes(),
                payload,
            )

    def test_names_and_destinations_are_checked_before_streaming(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"event")
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"outside")
        (directory / "unsafe.bin").symlink_to(outside)
        first = ChunkGuard(b"first")
        second = ChunkGuard(b"second")

        with self.assertRaisesRegex(ValueError, "duplicate upload basenames"):
            accept_request(
                "evt",
                [Upload("new.bin", first), Upload("new.bin", io.BytesIO(b"two"))],
            )
        with self.assertRaisesRegex(ValueError, "not a safe regular file"):
            accept_request(
                "evt",
                [Upload("new.bin", first), Upload("unsafe.bin", second)],
            )
        with self.assertRaises(ValueError):
            accept_request("evt", [Upload("nested/file", io.BytesIO(b"payload"))])

        self.assertEqual(first.read_sizes, [])
        self.assertEqual(second.read_sizes, [])
        self.assertFalse((directory / "new.bin").exists())
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual(self._queue_entries(), [])

    def test_new_upload_never_clobbers_a_late_existing_name(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"event")
        real_stream = submission._stream_upload

        def add_name_after_stream(
            private_directory: Path,
            upload: Upload,
            guard: object,
        ) -> Path:
            private_file = real_stream(private_directory, upload, guard)
            (directory / upload.basename).write_bytes(b"late caller bytes")
            return private_file

        with mock.patch.object(
            submission,
            "_stream_upload",
            side_effect=add_name_after_stream,
        ):
            with self.assertRaises(submission.InputPublicationError) as raised:
                accept_request("evt", [Upload("new.bin", io.BytesIO(b"upload"))])

        self.assertEqual((directory / "new.bin").read_bytes(), b"late caller bytes")
        self.assertIsInstance(raised.exception.__cause__, FileExistsError)
        self.assertIn(str(directory / "new.bin"), str(raised.exception))
        self.assertEqual(self._queue_entries(), [])
        self.assertEqual(self._private_queue_trees(), [])

    def test_rollback_failure_is_clear_chained_and_confined(self) -> None:
        directory = self._input_directory()
        target = directory / "event.xml"
        target.write_bytes(b"preceding event")
        outside = Path(self.temporary.name) / "outside-dir"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside unchanged")
        real_replace = submission._replace_input_name
        real_sync = submission._sync_input_directory
        sync_calls = 0
        rollback_calls = 0

        def fail_first_sync(directory_path: Path, guard: object) -> None:
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 1:
                raise OSError("injected input directory sync failure")
            real_sync(directory_path, guard)

        def fail_rollback(
            source: Path,
            destination: Path,
            source_guard: object,
            destination_guard: object,
        ) -> None:
            nonlocal rollback_calls
            if source.name.startswith(submission.UPLOAD_PRECEDING_PREFIX):
                rollback_calls += 1
                raise OSError("injected rollback failure")
            real_replace(source, destination, source_guard, destination_guard)

        with (
            mock.patch.object(
                submission,
                "_sync_input_directory",
                side_effect=fail_first_sync,
            ),
            mock.patch.object(
                submission,
                "_replace_input_name",
                side_effect=fail_rollback,
            ),
        ):
            with self.assertRaises(submission.InputPublicationError) as raised:
                accept_request(
                    "evt",
                    [Upload("event.xml", io.BytesIO(b"replacement"))],
                )

        self.assertEqual(rollback_calls, 1)
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn("input directory sync failure", str(raised.exception.__cause__))
        self.assertIn("one rollback", str(raised.exception))
        self.assertIn("injected rollback failure", str(raised.exception))
        self.assertEqual(self._queue_entries(), [])
        private_trees = self._private_queue_trees()
        self.assertEqual(len(private_trees), 1)
        backups = list(private_trees[0].glob(".uploads/preceding-*.tmp"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"preceding event")
        self.assertTrue(private_trees[0].is_relative_to(paths.queue_dir()))
        self.assertEqual(sentinel.read_bytes(), b"outside unchanged")
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["sentinel"])

    def test_private_upload_cleanup_failure_is_clear_and_confined(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"preceding event")
        outside = Path(self.temporary.name) / "outside-dir"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside unchanged")
        real_remove = submission._remove_private_upload

        def fail_private_removal(
            private_file: Path,
            guard: object,
            *,
            missing_ok: bool,
        ) -> None:
            if private_file.name.startswith(submission.UPLOAD_STREAM_PREFIX):
                raise OSError("injected private cleanup failure")
            real_remove(private_file, guard, missing_ok=missing_ok)

        with mock.patch.object(
            submission,
            "_remove_private_upload",
            side_effect=fail_private_removal,
        ):
            with self.assertRaises(submission.InputPublicationError) as raised:
                accept_request(
                    "evt",
                    [Upload("event.xml", FailingStream(b"new bytes"))],
                )

        self.assertEqual((directory / "event.xml").read_bytes(), b"preceding event")
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn("injected upload read failure", str(raised.exception.__cause__))
        self.assertIn("secondary private cleanup failure", str(raised.exception))
        self.assertIn("injected private cleanup failure", str(raised.exception))
        self.assertEqual(self._queue_entries(), [])
        private_trees = self._private_queue_trees()
        self.assertEqual(len(private_trees), 1)
        self.assertTrue(private_trees[0].is_relative_to(paths.queue_dir()))
        self.assertEqual(sentinel.read_bytes(), b"outside unchanged")
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["sentinel"])

    def test_cross_filesystem_private_storage_fails_before_streaming(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"preceding event")
        stream = ChunkGuard(b"must not be read")
        real_fstat = submission.os.fstat
        real_verify = submission._verify_atomic_upload_filesystem

        def report_other_device(input_guard: object, upload_guard: object) -> None:
            def changed_fstat(descriptor: int):
                details = real_fstat(descriptor)
                if descriptor == upload_guard.descriptor:
                    values = list(details)
                    values[2] = details.st_dev + 1
                    return os.stat_result(values)
                return details

            with mock.patch.object(submission.os, "fstat", side_effect=changed_fstat):
                real_verify(input_guard, upload_guard)

        with mock.patch.object(
            submission,
            "_verify_atomic_upload_filesystem",
            side_effect=report_other_device,
        ):
            with self.assertRaises(submission.InputPublicationError) as raised:
                accept_request("evt", [Upload("extra.bin", stream)])

        self.assertEqual(stream.read_sizes, [])
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn("different filesystems", str(raised.exception.__cause__))
        self.assertFalse((directory / "extra.bin").exists())
        self.assertEqual(self._queue_entries(), [])
        self.assertEqual(self._private_queue_trees(), [])

    def test_snapshot_is_immutable_after_caller_changes(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"accepted")
        result = accept_request("evt")

        (directory / "event.xml").write_bytes(b"later change")
        (directory / "later.bin").write_bytes(b"later file")

        snapshot = paths.queue_request_dir(result.internal_sequence)
        self.assertEqual((snapshot / "event.xml").read_bytes(), b"accepted")
        self.assertFalse((snapshot / "later.bin").exists())

    def test_snapshot_failure_keeps_upload_and_accepts_nothing(self) -> None:
        directory = self._input_directory()
        (directory / "manual.bin").write_bytes(b"manual")

        with mock.patch.object(
            submission,
            "_copy_regular_file",
            side_effect=OSError("injected copy failure"),
        ):
            with self.assertRaises(submission.InputSnapshotError) as raised:
                accept_request(
                    "evt",
                    [Upload("event.xml", io.BytesIO(b"published event"))],
                )

        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn("injected copy failure", str(raised.exception.__cause__))

        self.assertEqual((directory / "event.xml").read_bytes(), b"published event")
        self.assertEqual((directory / "manual.bin").read_bytes(), b"manual")
        self.assertEqual(self._queue_entries(), [])
        self.assertEqual(self._private_queue_trees(), [])

    def test_manifest_matches_exact_snapshot_bytes(self) -> None:
        directory = self._input_directory()
        directory.joinpath("event.xml").write_bytes(b"event")
        directory.joinpath("odd name.数据").write_bytes(b"\x00\xffopaque")
        result = accept_request("evt")

        manifest = json.loads(
            (paths.queue_entry_dir(result.internal_sequence) / "request-manifest.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["basename"] for item in manifest["files"]],
            ["event.xml", "odd name.数据"],
        )
        self.assertEqual(
            (paths.queue_request_dir(result.internal_sequence) / "odd name.数据")
            .read_bytes(),
            b"\x00\xffopaque",
        )


    def test_stream_failure_changes_no_caller_basename(self) -> None:
        for basename, preceding in (
            ("event.xml", b"preceding event"),
            ("new.bin", None),
        ):
            with self.subTest(basename=basename):
                event_id = basename.replace(".", "-")
                directory = self._input_directory(event_id)
                (directory / "event.xml").write_bytes(b"required event")
                target = directory / basename
                if preceding is not None:
                    target.write_bytes(preceding)

                with self.assertRaises(submission.InputPublicationError) as raised:
                    accept_request(
                        event_id,
                        [Upload(basename, FailingStream(b"new bytes"))],
                    )

                if preceding is None:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(target.read_bytes(), preceding)
                self.assertIsInstance(raised.exception.__cause__, OSError)
                self.assertIn("injected upload read failure", str(raised.exception))
                self.assertEqual(self._queue_entries(), [])
                self.assertEqual(self._private_queue_trees(), [])

    def test_replacement_rename_failure_rolls_back_once(self) -> None:
        directory = self._input_directory()
        target = directory / "event.xml"
        target.write_bytes(b"preceding event")
        real_replace = submission._replace_input_name
        sources: list[str] = []

        def fail_first_replace(
            source: Path,
            destination: Path,
            source_guard: object,
            destination_guard: object,
        ) -> None:
            sources.append(source.name)
            if len(sources) == 1:
                raise OSError("injected replacement rename failure")
            real_replace(source, destination, source_guard, destination_guard)

        with mock.patch.object(
            submission,
            "_replace_input_name",
            side_effect=fail_first_replace,
        ):
            with self.assertRaises(submission.InputPublicationError) as raised:
                accept_request(
                    "evt",
                    [Upload("event.xml", io.BytesIO(b"replacement"))],
                )

        self.assertEqual(len(sources), 2)
        self.assertTrue(sources[1].startswith(submission.UPLOAD_PRECEDING_PREFIX))
        self.assertEqual(target.read_bytes(), b"preceding event")
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn("preceding bytes were restored", str(raised.exception))
        self.assertEqual(self._queue_entries(), [])
        self.assertEqual(self._private_queue_trees(), [])

    def test_replacement_input_sync_failure_rolls_back_once(self) -> None:
        directory = self._input_directory()
        target = directory / "event.xml"
        target.write_bytes(b"preceding event")
        real_sync = submission._sync_input_directory
        real_replace = submission._replace_input_name
        sync_calls = 0
        rollback_calls = 0

        def fail_first_sync(directory_path: Path, guard: object) -> None:
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 1:
                raise OSError("injected input directory sync failure")
            real_sync(directory_path, guard)

        def count_rollback(
            source: Path,
            destination: Path,
            source_guard: object,
            destination_guard: object,
        ) -> None:
            nonlocal rollback_calls
            if source.name.startswith(submission.UPLOAD_PRECEDING_PREFIX):
                rollback_calls += 1
            real_replace(source, destination, source_guard, destination_guard)

        with (
            mock.patch.object(
                submission,
                "_sync_input_directory",
                side_effect=fail_first_sync,
            ),
            mock.patch.object(
                submission,
                "_replace_input_name",
                side_effect=count_rollback,
            ),
        ):
            with self.assertRaises(submission.InputPublicationError) as raised:
                accept_request(
                    "evt",
                    [Upload("event.xml", io.BytesIO(b"replacement"))],
                )

        self.assertEqual(rollback_calls, 1)
        self.assertEqual(sync_calls, 2)
        self.assertEqual(target.read_bytes(), b"preceding event")
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertEqual(self._queue_entries(), [])
        self.assertEqual(self._private_queue_trees(), [])

    def test_record_write_failure_leaves_no_queue_entry(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"event")
        real_write_json = submission._write_json_sync

        def fail_status_write(
            target: Path,
            data: dict[str, object],
            directory_descriptor: object = None,
        ) -> None:
            if target.name == "status.json":
                raise OSError("injected record write failure")
            real_write_json(target, data, directory_descriptor)

        with mock.patch.object(
            submission,
            "_write_json_sync",
            side_effect=fail_status_write,
        ):
            with self.assertRaisesRegex(OSError, "injected record write failure"):
                accept_request("evt")

        self.assertEqual(self._queue_entries(), [])
        self.assertEqual(self._private_queue_trees(), [])

    def test_queue_parent_sync_failure_reports_complete_entry(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"event")
        real_sync = submission._sync_published_queue_parent

        def fail_after_publication(directory_path: Path, guard: object) -> None:
            if directory_path == paths.queue_dir() and self._queue_entries():
                raise OSError("injected queue sync failure")
            real_sync(directory_path, guard)

        with mock.patch.object(
            submission,
            "_sync_published_queue_parent",
            side_effect=fail_after_publication,
        ):
            with self.assertRaisesRegex(OSError, "injected queue sync failure"):
                accept_request("evt")

        queued, malformed = discover_queue()
        self.assertEqual(
            [(record.event_id, record.internal_sequence) for record in queued],
            [("evt", 1)],
        )
        self.assertEqual(malformed, [])
        self.assertEqual(accept_request("evt").internal_sequence, 2)

    def test_sequence_gap_is_not_reused_after_queue_rename_failure(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"event")
        real_rename = submission.os.rename

        def fail_queue_rename(
            source: object,
            destination: object,
            **kwargs: object,
        ) -> None:
            if (
                Path(source).name.startswith(".accept-")
                and Path(destination).name.isdigit()
            ):
                raise OSError("injected queue rename failure")
            real_rename(source, destination, **kwargs)

        with mock.patch.object(submission.os, "rename", side_effect=fail_queue_rename):
            with self.assertRaisesRegex(OSError, "injected queue rename failure"):
                accept_request("evt")

        self.assertEqual(self._queue_entries(), [])
        self.assertEqual(
            paths.queue_sequence_file().read_text(encoding="ascii"),
            "2\n",
        )
        self.assertEqual(accept_request("evt").internal_sequence, 2)

    def test_complete_entry_appears_after_one_queue_rename(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"event")
        rename_waiting = Event()
        allow_rename = Event()
        queue_renames = 0
        real_rename = submission.os.rename

        def pause_before_publication(
            source: object,
            destination: object,
            **kwargs: object,
        ) -> None:
            nonlocal queue_renames
            if (
                Path(source).name.startswith(".accept-")
                and Path(destination).name.isdigit()
            ):
                queue_renames += 1
                rename_waiting.set()
                if not allow_rename.wait(timeout=5):
                    raise AssertionError("queue rename release was not signaled")
            real_rename(source, destination, **kwargs)

        with mock.patch.object(
            submission.os,
            "rename",
            side_effect=pause_before_publication,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                acceptance = executor.submit(accept_request, "evt")
                self.assertTrue(rename_waiting.wait(timeout=5))
                self.assertEqual(self._queue_entries(), [])
                discovery = executor.submit(status.scan_queue_records)
                with self.assertRaises(TimeoutError):
                    discovery.result(timeout=0.1)
                allow_rename.set()
                accepted = acceptance.result(timeout=5)
                records, malformed = discovery.result(timeout=5)

        self.assertEqual(queue_renames, 1)
        self.assertEqual(accepted.internal_sequence, 1)
        self.assertEqual([record.internal_sequence for record in records], [1])
        self.assertEqual(malformed, [])

    def test_sequences_are_monotonic_and_reload(self) -> None:
        for event_id in ("first", "second"):
            self._input_directory(event_id).joinpath("event.xml").write_bytes(
                event_id.encode("ascii")
            )

        first = accept_request("first")
        second = accept_request("second")
        self.assertEqual((first.internal_sequence, second.internal_sequence), (1, 2))

        reloaded_queue = importlib.reload(
            importlib.import_module("shakemap_service.queue")
        )
        records, malformed = reloaded_queue.discover_queue()
        self.assertEqual([record.internal_sequence for record in records], [1, 2])
        self.assertEqual(malformed, [])

    def test_concurrent_acceptance_allocates_unique_sequences(self) -> None:
        event_ids = [f"event-{index}" for index in range(8)]
        for event_id in event_ids:
            self._input_directory(event_id).joinpath("event.xml").write_bytes(
                event_id.encode("ascii")
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(accept_request, event_ids))

        self.assertEqual(
            sorted(result.internal_sequence for result in results),
            list(range(1, 9)),
        )
        records, malformed = discover_queue()
        self.assertEqual(
            [record.internal_sequence for record in records],
            list(range(1, 9)),
        )
        self.assertEqual(malformed, [])

    def test_concurrent_same_event_uploads_snapshot_their_own_bytes(self) -> None:
        input_directory = self._input_directory()
        input_directory.joinpath("event.xml").write_bytes(b"initial")
        original_snapshot = submission._snapshot_inputs
        first_waiting = Event()
        release_first = Event()

        def controlled_snapshot(
            *args: object,
            **kwargs: object,
        ) -> list[dict[str, object]]:
            if not first_waiting.is_set():
                first_waiting.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("first snapshot was not released")
            return original_snapshot(*args, **kwargs)

        with mock.patch.object(submission, "_snapshot_inputs", controlled_snapshot):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(
                    accept_request,
                    "evt",
                    [Upload("event.xml", io.BytesIO(b"A-bytes"))],
                )
                self.assertTrue(first_waiting.wait(timeout=5))
                second_future = executor.submit(
                    accept_request,
                    "evt",
                    [Upload("event.xml", io.BytesIO(b"B-bytes"))],
                )
                with self.assertRaises(TimeoutError):
                    second_future.result(timeout=0.1)
                release_first.set()
                first = first_future.result(timeout=5)
                second = second_future.result(timeout=5)

        self.assertEqual(
            paths.queue_request_dir(first.internal_sequence)
            .joinpath("event.xml")
            .read_bytes(),
            b"A-bytes",
        )
        self.assertEqual(
            paths.queue_request_dir(second.internal_sequence)
            .joinpath("event.xml")
            .read_bytes(),
            b"B-bytes",
        )

    def test_different_event_acceptance_remains_concurrent(self) -> None:
        first_directory = self._input_directory("first")
        first_directory.joinpath("event.xml").write_bytes(b"first")
        second_directory = self._input_directory("second")
        second_directory.joinpath("event.xml").write_bytes(b"second")
        original_snapshot = submission._snapshot_inputs
        first_waiting = Event()
        release_first = Event()
        second_finished = Event()

        def controlled_snapshot(
            *args: object,
            **kwargs: object,
        ) -> list[dict[str, object]]:
            input_directory = args[0]
            if input_directory == first_directory:
                first_waiting.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("first snapshot was not released")
            result = original_snapshot(*args, **kwargs)
            if input_directory == second_directory:
                second_finished.set()
            return result

        with mock.patch.object(submission, "_snapshot_inputs", controlled_snapshot):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(accept_request, "first")
                self.assertTrue(first_waiting.wait(timeout=5))
                second_future = executor.submit(accept_request, "second")
                self.assertTrue(second_finished.wait(timeout=5))
                second = second_future.result(timeout=5)
                release_first.set()
                first = first_future.result(timeout=5)

        self.assertEqual(
            paths.queue_request_dir(first.internal_sequence)
            .joinpath("event.xml")
            .read_bytes(),
            b"first",
        )
        self.assertEqual(
            paths.queue_request_dir(second.internal_sequence)
            .joinpath("event.xml")
            .read_bytes(),
            b"second",
        )

    def test_same_id_acceptance_does_not_change_preceding_calculation(self) -> None:
        directory = self._input_directory()
        (directory / "event.xml").write_bytes(b"new request")
        native_tree = paths.event_products_dir("evt") / "current"
        native_tree.mkdir(parents=True)
        native_tree.joinpath("native.bin").write_bytes(b"preceding native")
        service_tree = paths.event_service_dir("evt")
        service_tree.mkdir(parents=True)
        service_tree.joinpath("sentinel.json").write_bytes(b"preceding service")

        first = accept_request("evt", overwrite=True)
        second = accept_request("evt", overwrite=False)

        self.assertEqual(
            (native_tree / "native.bin").read_bytes(),
            b"preceding native",
        )
        self.assertEqual(
            (service_tree / "sentinel.json").read_bytes(),
            b"preceding service",
        )
        self.assertEqual((first.internal_sequence, second.internal_sequence), (1, 2))


if __name__ == "__main__":
    unittest.main()
