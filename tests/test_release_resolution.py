#!/usr/bin/env python3
"""Deterministic host tests for immutable official release resolution."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from shakemap_service import release


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class StableReleaseTests(unittest.TestCase):
    def test_semantic_version_ordering_not_lexical(self) -> None:
        metadata = [
            {"tag_name": "v4.4.9"},
            {"tag_name": "v4.4.10"},
            {"tag_name": "v4.10.0"},
            {"tag_name": "v5.0.0"},
        ]
        self.assertEqual(release.select_latest_stable_release(metadata), "v5.0.0")

    def test_prereleases_and_moving_refs_are_rejected(self) -> None:
        for tag in ("main", "v4.4.10-rc1", "v4.4.10.dev2", "v4.4.10-alpha", "4.4"):
            with self.subTest(tag=tag), self.assertRaises(release.ReleaseResolutionError):
                release.stable_version(tag)
        self.assertEqual(
            release.select_latest_stable_release(
                [{"tag_name": "v4.4.10-rc1"}, {"tag_name": "main"}, {"tag_name": "v4.4.9"}]
            ),
            "v4.4.9",
        )

    def test_malformed_no_result_and_ambiguous_metadata_fail_closed(self) -> None:
        invalid = (
            {},
            [],
            [{"name": "missing tag"}],
            [{"tag_name": "v4.4.10-rc1"}],
            [{"tag_name": "v4.4.10"}, {"tag_name": "4.4.10"}],
        )
        for metadata in invalid:
            with self.subTest(metadata=metadata), self.assertRaises(release.ReleaseResolutionError):
                release.select_latest_stable_release(metadata)

    def test_lightweight_and_annotated_tags_resolve(self) -> None:
        self.assertEqual(
            release.resolve_tag_commit_from_ls_remote(
                "v4.4.10", f"{COMMIT_A}\trefs/tags/v4.4.10\n"
            ),
            COMMIT_A,
        )
        annotated = (
            f"{COMMIT_A}\trefs/tags/v4.4.10\n"
            f"{COMMIT_B}\trefs/tags/v4.4.10^{{}}\n"
        )
        self.assertEqual(
            release.resolve_tag_commit_from_ls_remote("v4.4.10", annotated), COMMIT_B
        )

    def test_full_commit_validation(self) -> None:
        self.assertEqual(release.validate_full_commit(COMMIT_A.upper()), COMMIT_A)
        for value in ("abc123", "g" * 40, COMMIT_A + "0"):
            with self.subTest(value=value), self.assertRaises(release.ReleaseResolutionError):
                release.validate_full_commit(value)

    def test_requested_stable_tag_resolves_its_official_commit(self) -> None:
        with patch.object(release, "query_official_tag", return_value=COMMIT_A):
            resolved = release.resolve_official_release_tag("v4.4.10")
            self.assertEqual(resolved, release.ResolvedRelease(tag="v4.4.10", commit=COMMIT_A))
        with self.assertRaises(release.ReleaseResolutionError):
            release.resolve_official_release_tag("main")

if __name__ == "__main__":
    unittest.main(verbosity=2)
