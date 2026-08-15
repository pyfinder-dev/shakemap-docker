#!/usr/bin/env python3
"""Focused host tests for inert startup while managed execution is disabled."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shakemap_service import main, paths
from shakemap_service.config import Settings


class DisabledApplicationStartupTests(unittest.TestCase):
    def test_startup_does_not_recover_start_worker_or_change_records(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inert_startup_") as temporary:
            service_root = Path(temporary) / "shakemap"
            fixture = service_root / ".service/events/00000000000000000001/status.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_bytes(
                b'{"event_id":"fixture","status":"RUNNING","opaque":"preserve"}\n'
            )
            queue_fixture = service_root / ".service/events/.next-sequence"
            queue_fixture.write_bytes(b"00000000000000000002\n")
            before = {
                fixture: fixture.read_bytes(),
                queue_fixture: queue_fixture.read_bytes(),
            }

            async def exercise_lifespan() -> None:
                async with main.lifespan(main.app):
                    self.assertEqual(
                        {path: path.read_bytes() for path in before},
                        before,
                    )

            with (
                patch.object(
                    paths,
                    "settings",
                    Settings(runtime_root=str(service_root.parent)),
                ),
                patch.object(
                    main,
                    "recover_interrupted_events",
                    side_effect=AssertionError("startup recovery was invoked"),
                ) as recovery,
                patch.object(
                    main.threading,
                    "Thread",
                    side_effect=AssertionError("worker thread was constructed"),
                ) as thread,
            ):
                asyncio.run(exercise_lifespan())

            recovery.assert_not_called()
            thread.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
