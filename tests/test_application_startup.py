#!/usr/bin/env python3
"""Host tests for durable calculation recovery during application startup."""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import main, paths, startup_recovery, status, worker
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


class ApplicationStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="startup_recovery_")
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

    def _running(self, event_id: str, *, current: bool = False):
        record = accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(event_id.encode()))],
        )
        status.transition_to_running(record.internal_sequence)
        if current:
            paths.events_dir().mkdir(parents=True, exist_ok=True)
            paths.queue_entry_dir(record.internal_sequence).rename(
                paths.event_service_dir(event_id)
            )
        return record

    def _run_lifespan(self, inside=lambda: None) -> None:
        async def exercise() -> None:
            async with main.lifespan(main.app):
                inside()
        asyncio.run(exercise())

    def _snapshot(self, directory: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }

    def _snapshots(self, records) -> dict[int, dict[str, bytes]]:
        return {
            item.internal_sequence: self._snapshot(
                paths.queue_entry_dir(item.internal_sequence)
            )
            for item in records
        }

    def test_recovers_in_order_preserves_queue_and_is_idempotent(self) -> None:
        queued_running = self._running("queue-event")
        current_running = self._running("current-event", current=True)
        waiting = [
            accept_request("current-event", [Upload("event.xml", io.BytesIO(b"a"))]),
            accept_request("other-event", [Upload("event.xml", io.BytesIO(b"b"))]),
        ]
        preserved = self._snapshots(waiting)
        sequence_bytes = paths.queue_sequence_file().read_bytes()
        real_recovery = startup_recovery.recalculation.recover_stale_running_calculation

        with (
            mock.patch.object(
                startup_recovery.recalculation,
                "recover_stale_running_calculation",
                wraps=real_recovery,
            ) as recovered,
            mock.patch.object(worker, "run_worker_cycle") as worker_cycle,
            mock.patch.object(worker, "execute_shakemap") as native_execution,
        ):
            self._run_lifespan()
            self.assertEqual(
                status.read_status(queued_running.internal_sequence).status,
                "FAILED",
            )
            self.assertEqual(status.read_current_record("current-event").status, "FAILED")
            terminal_bytes = paths.queue_status_file(
                queued_running.internal_sequence
            ).read_bytes()
            self._run_lifespan()

        self.assertEqual(recovered.call_args_list, [
            mock.call("queue-event", queued_running.internal_sequence),
            mock.call("current-event", current_running.internal_sequence),
        ])
        queued = [
            item
            for item in status.scan_queue_records()[0]
            if item.status == "QUEUED"
        ]
        self.assertEqual([item.internal_sequence for item in queued],
                         [item.internal_sequence for item in waiting])
        self.assertEqual(self._snapshots(waiting), preserved)
        self.assertEqual(paths.queue_sequence_file().read_bytes(), sequence_bytes)
        self.assertEqual(paths.queue_status_file(
            queued_running.internal_sequence).read_bytes(), terminal_bytes)
        worker_cycle.assert_not_called()
        native_execution.assert_not_called()

    def test_malformed_state_refuses_startup_before_recovery(self) -> None:
        running = self._running("running")
        malformed = paths.queue_entry_dir(99)
        malformed.mkdir(parents=True)
        malformed.joinpath("status.json").write_text("{broken", encoding="utf-8")
        entered: list[bool] = []
        with mock.patch.object(
            startup_recovery.recalculation, "recover_stale_running_calculation"
        ) as recovered, self.assertRaisesRegex(
            startup_recovery.StartupRecoveryError, "malformed durable state"
        ):
            self._run_lifespan(lambda: entered.append(True))
        recovered.assert_not_called()
        self.assertEqual(entered, [])
        self.assertEqual(status.read_status(running.internal_sequence).status, "RUNNING")

    def test_duplicate_sequence_refuses_startup_before_recovery(self) -> None:
        queued = self._running("queued-event")
        self._running("current-event", current=True)
        current_dir = paths.event_service_dir("current-event")
        for target in (current_dir / "status.json", current_dir / "request-manifest.json"):
            payload = json.loads(target.read_text(encoding="utf-8"))
            payload["internal_sequence"] = queued.internal_sequence
            target.write_text(json.dumps(payload), encoding="utf-8")
        with mock.patch.object(
            startup_recovery.recalculation, "recover_stale_running_calculation"
        ) as recovered, self.assertRaisesRegex(
            startup_recovery.StartupRecoveryError, "duplicate durable internal sequence"
        ):
            self._run_lifespan()
        recovered.assert_not_called()
        self.assertEqual(status.read_status(queued.internal_sequence).status, "RUNNING")

    def test_irreconcilable_or_unresolved_running_refuses_startup(self) -> None:
        running = self._running("running")
        for result, message in (
            (RuntimeError("irreconcilable"), "could not be recovered"),
            (False, "remain after recovery"),
        ):
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {
                "return_value": result
            }
            with self.subTest(message=message), mock.patch.object(
                startup_recovery.recalculation,
                "recover_stale_running_calculation",
                **kwargs,
            ), self.assertRaisesRegex(startup_recovery.StartupRecoveryError, message):
                self._run_lifespan()
        self.assertEqual(status.read_status(running.internal_sequence).status, "RUNNING")


if __name__ == "__main__":
    unittest.main(verbosity=2)
