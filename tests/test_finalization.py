#!/usr/bin/env python3
"""Host tests for exclusive finalization and bootstrap readiness."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest import mock

from shakemap_service import finalization, paths, readiness, status, submission
from shakemap_service.config import Settings
from shakemap_service.scheduler import Scheduler
from shakemap_service.submission import Upload


def identity(image: str = "b") -> dict[str, object]:
    return {
        "immutable_image": {
            "available": True,
            "upstream": {"release_tag": "v4.4.9", "source_commit": "a" * 40},
            "installed": {"shakemap_distribution_version": "4.4.9"},
        },
        "deployment": {"available": True, "image_id": "sha256:" + image * 64},
    }


class FinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_paths = paths.settings
        self.previous_status = status.settings
        configured = Settings(runtime_root=self.temporary.name)
        paths.settings = configured
        status.settings = configured
        readiness._set_provisional_ready(False)
        self.identity = identity()

    def tearDown(self) -> None:
        readiness._set_provisional_ready(False)
        status.settings = self.previous_status
        paths.settings = self.previous_paths
        self.temporary.cleanup()

    def _ready(self) -> None:
        readiness._record_ready(self.identity)

    def test_runtime_preparation_rejects_linked_data_roots(self) -> None:
        data_root = paths.shakemap_data_dir()
        data_root.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside-regional"
        outside.mkdir()
        paths.regional_data_dir().symlink_to(outside, target_is_directory=True)
        seeds = Path(self.temporary.name) / "seeds"
        seeds.mkdir()
        seeds.joinpath("region").mkdir()

        with self.assertRaisesRegex(ValueError, "unsafe service directory ancestry"):
            finalization.prepare_runtime(seeds)

        self.assertEqual(list(outside.iterdir()), [])

    def test_runtime_preparation_seeds_only_missing_regions(self) -> None:
        destination = paths.regional_data_dir()
        existing = destination / "existing"
        existing.mkdir(parents=True)
        existing.joinpath("operator.conf").write_text("keep", encoding="utf-8")
        seeds = Path(self.temporary.name) / "seeds"
        seeds.joinpath("existing").mkdir(parents=True)
        seeds.joinpath("existing/seed.conf").write_text("replace", encoding="utf-8")
        seeds.joinpath("new").mkdir()
        seeds.joinpath("new/seed.conf").write_text("copy", encoding="utf-8")

        seeded = finalization.prepare_runtime(seeds)

        self.assertEqual(seeded, ["new"])
        self.assertEqual(existing.joinpath("operator.conf").read_text(), "keep")
        self.assertFalse(existing.joinpath("seed.conf").exists())
        self.assertEqual(destination.joinpath("new/seed.conf").read_text(), "copy")

    def test_interrupted_region_copy_is_absent_and_retry_publishes_complete_seed(self) -> None:
        seeds = Path(self.temporary.name) / "seeds"
        seeds.joinpath("new").mkdir(parents=True)
        seeds.joinpath("new/first.conf").write_text("first", encoding="utf-8")
        seeds.joinpath("new/second.conf").write_text("second", encoding="utf-8")
        original = finalization.shutil.copytree

        def partial_copy(source: Path, target: Path, **_kwargs: object) -> None:
            target.mkdir()
            target.joinpath("first.conf").write_text("first", encoding="utf-8")
            raise OSError("injected interrupted copy")

        with mock.patch.object(
            finalization.shutil,
            "copytree",
            side_effect=partial_copy,
        ):
            with self.assertRaisesRegex(OSError, "injected interrupted copy"):
                finalization.prepare_runtime(seeds)

        destination = paths.regional_data_dir()
        self.assertFalse(destination.joinpath("new").exists())
        self.assertEqual(
            [item.name for item in destination.iterdir() if item.name.startswith(".")],
            [],
        )
        destination.joinpath(".regional-seed-crash-remnant").mkdir()
        with mock.patch.object(finalization.shutil, "copytree", wraps=original):
            self.assertEqual(finalization.prepare_runtime(seeds), ["new"])
        self.assertEqual(destination.joinpath("new/first.conf").read_text(), "first")
        self.assertEqual(destination.joinpath("new/second.conf").read_text(), "second")

    def test_begin_refuses_malformed_or_unfinished_state_without_changing_readiness(self) -> None:
        self._ready()
        queue = paths.queue_dir()
        queue.mkdir(parents=True, exist_ok=True)
        malformed = queue / paths.queue_entry_name(1)
        malformed.mkdir()
        malformed.joinpath("status.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(finalization.FinalizationError, "malformed"):
            finalization.begin()
        self.assertTrue(readiness.read_readiness(self.identity)["ready"])

        malformed.joinpath("status.json").unlink()
        malformed.rmdir()
        accepted = submission.accept_request(
            "waiting",
            [Upload("event.xml", io.BytesIO(b"event"))],
        )
        with self.assertRaisesRegex(finalization.FinalizationError, "unfinished"):
            finalization.begin()
        self.assertEqual(status.read_status(accepted.internal_sequence).status, "QUEUED")
        self.assertTrue(readiness.read_readiness(self.identity)["ready"])

    def test_submission_publication_and_begin_share_one_exclusion_boundary(self) -> None:
        self._ready()
        entered = Event()
        release = Event()
        original = submission._publish_queue_entry_locked
        accepted: list[object] = []
        failures: list[BaseException] = []

        def delayed(*args: object, **kwargs: object):
            entered.set()
            if not release.wait(5):
                raise AssertionError("publication was not released")
            return original(*args, **kwargs)

        def publish() -> None:
            try:
                accepted.append(
                    submission.accept_ready_request(
                        "race",
                        [Upload("event.xml", io.BytesIO(b"event"))],
                        readiness_reader=lambda: readiness.read_readiness(self.identity),
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        publication = Thread(target=publish)
        with mock.patch.object(
            submission,
            "_publish_queue_entry_locked",
            side_effect=delayed,
        ):
            publication.start()
            self.assertTrue(entered.wait(5))
            result: list[BaseException] = []

            def start_finalization() -> None:
                try:
                    finalization.begin()
                except BaseException as exc:
                    result.append(exc)

            gate = Thread(target=start_finalization)
            gate.start()
            self.assertTrue(gate.is_alive())
            release.set()
            publication.join(5)
            gate.join(5)

        self.assertFalse(failures)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], finalization.FinalizationError)
        self.assertIn("unfinished", str(result[0]))

    def test_marker_is_consumed_once_and_cannot_survive_restart_or_mismatch(self) -> None:
        finalization.begin()
        image_id = self.identity["deployment"]["image_id"]
        finalization.arm_bootstrap_marker(image_id)
        self.assertTrue(paths.bootstrap_marker_file().is_file())
        self.assertTrue(finalization.consume_bootstrap_marker(self.identity))
        self.assertFalse(paths.bootstrap_marker_file().exists())
        self.assertTrue(readiness.read_readiness(self.identity)["ready"])
        self.assertFalse(finalization.consume_bootstrap_marker(self.identity))
        self.assertFalse(readiness.read_readiness(self.identity)["ready"])

        finalization.arm_bootstrap_marker(image_id)
        self.assertFalse(finalization.consume_bootstrap_marker(identity("c")))
        self.assertFalse(paths.bootstrap_marker_file().exists())
        self.assertFalse(readiness.read_readiness(self.identity)["ready"])

    def test_malformed_marker_is_consumed_without_provisional_readiness(self) -> None:
        finalization.begin()
        marker = paths.bootstrap_marker_file()
        marker.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        self.assertFalse(finalization.consume_bootstrap_marker(self.identity))
        self.assertFalse(marker.exists())
        self.assertFalse(readiness.read_readiness(self.identity)["ready"])

    def test_oversized_regular_marker_is_consumed_before_validation(self) -> None:
        finalization.begin()
        marker = paths.bootstrap_marker_file()
        marker.write_bytes(b"x" * (finalization.MAX_MARKER_BYTES + 1))
        self.assertFalse(finalization.consume_bootstrap_marker(self.identity))
        self.assertFalse(marker.exists())
        self.assertFalse(readiness.read_readiness(self.identity)["ready"])

    def test_provisional_bootstrap_uses_ordinary_submission_and_scheduler(self) -> None:
        finalization.begin()
        finalization.arm_bootstrap_marker(self.identity["deployment"]["image_id"])
        self.assertTrue(finalization.consume_bootstrap_marker(self.identity))
        accepted = submission.accept_ready_request(
            "bootstrap",
            [Upload("event.xml", io.BytesIO(b"event"))],
            readiness_reader=lambda: readiness.read_readiness(self.identity),
        )

        def complete(record: status.CalculationRecord) -> None:
            status.transition_to_failed(
                record.internal_sequence,
                "fixture completion",
                code="fixture_completion",
            )

        scheduler = Scheduler(complete, service_settings=paths.settings)
        try:
            self.assertEqual(
                [record.internal_sequence for record in scheduler.tick()],
                [accepted.internal_sequence],
            )
            self.assertTrue(scheduler.wait_until_idle(timeout=5))
        finally:
            scheduler.shutdown()
        self.assertEqual(
            status.read_status(accepted.internal_sequence).status,
            status.LifecycleState.FAILED.value,
        )
        self.assertTrue(readiness.read_readiness(self.identity)["ready"])
        finalization.record_failure("verification calculation failed")
        state = readiness.read_readiness(self.identity)
        self.assertFalse(state["ready"])
        self.assertEqual(state["reason"], "verification calculation failed")

    def test_ready_commit_requires_finalizing_and_binds_complete_identity(self) -> None:
        with self.assertRaisesRegex(finalization.FinalizationError, "not in finalizing"):
            finalization.record_ready(self.identity)
        finalization.begin()
        finalization.record_ready(self.identity)
        self.assertTrue(readiness.read_readiness(self.identity)["ready"])
        expected = readiness._current_identity(self.identity)
        self.assertTrue(
            finalization.recorded_identity_matches(
                image_id=expected["image_id"],
                release_tag=expected["release_tag"],
                source_commit=expected["source_commit"],
                shakemap_version=expected["shakemap_version"],
            )
        )
        with mock.patch.dict(
            finalization.preparation.GLOBAL_ASSETS["vs30"],
            {"sha256": "f" * 64},
        ):
            self.assertFalse(
                finalization.recorded_identity_matches(
                    image_id=expected["image_id"],
                    release_tag=expected["release_tag"],
                    source_commit=expected["source_commit"],
                    shakemap_version=expected["shakemap_version"],
                )
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
