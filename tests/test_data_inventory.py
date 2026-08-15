#!/usr/bin/env python3
"""Focused host tests for cheap, read-only external-data inventory."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shakemap_service import preparation


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes() if path.is_file() else None
        )
        for path in sorted(root.rglob("*"))
    }


class DataInventoryTests(unittest.TestCase):
    def test_inventory_reports_contracted_paths_without_writes_or_hashes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="data_inventory_") as temporary:
            root = Path(temporary)
            vs30 = root / "global/vs30/global_vs30.grd"
            topo = root / "global/topo/topo_30sec.grd"
            regional = root / "regional/local"
            for directory in (vs30.parent, topo.parent, regional):
                directory.mkdir(parents=True)
            vs30.write_bytes(b"vs30 fixture")
            topo.write_bytes(b"topography fixture")
            before = snapshot(root)

            with patch.object(
                preparation,
                "sha256",
                side_effect=AssertionError("inventory hashed an operator dataset"),
            ):
                result = preparation.inspect_data_assets(root)

            self.assertEqual(snapshot(root), before)
            self.assertEqual(
                set(result["assets"]), {"global_vs30", "global_topography"}
            )
            self.assertTrue(
                all(
                    asset["present"] and asset["readable"]
                    for asset in result["assets"].values()
                )
            )
            self.assertEqual(result["summary"]["validation_state"], "not_evaluated")
            self.assertEqual(
                result["summary"]["compatibility_state"], "not_evaluated"
            )
            self.assertEqual(result["summary"]["coverage_state"], "not_evaluated")
            self.assertEqual(result["summary"]["actual_use_state"], "not_evaluated")
            self.assertEqual(
                [item["name"] for item in result["configurations"]],
                ["global", "local"],
            )

    def test_missing_root_is_not_created(self) -> None:
        with tempfile.TemporaryDirectory(prefix="missing_inventory_") as temporary:
            root = Path(temporary) / "does-not-exist"
            result = preparation.inspect_data_assets(root)
            self.assertFalse(root.exists())
            self.assertTrue(
                all(
                    not asset["present"] and not asset["readable"]
                    for asset in result["assets"].values()
                )
            )
            self.assertEqual(result["summary"]["validation_state"], "not_evaluated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
