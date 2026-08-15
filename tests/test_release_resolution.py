#!/usr/bin/env python3
"""Deterministic host tests for the declared immutable release."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shakemap_service import release


PROJECT_DIR = Path(__file__).resolve().parent.parent
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class DeclaredReleaseTests(unittest.TestCase):
    def test_repository_declares_supported_release(self) -> None:
        self.assertEqual(
            release.load_declared_release_tag(PROJECT_DIR / "VERSIONS.env"),
            "v4.4.9",
        )

    def test_prereleases_and_moving_refs_are_rejected(self) -> None:
        for tag in ("main", "v4.4.9-rc1", "v4.4.9.dev2", "v4.4.9-alpha", "4.4"):
            with self.subTest(tag=tag), self.assertRaises(release.ReleaseResolutionError):
                release.stable_version(tag)

    def test_malformed_missing_and_duplicate_declarations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release_declaration_") as temporary:
            path = Path(temporary) / "VERSIONS.env"
            for content in (
                "",
                "SHAKEMAP_RELEASE_TAG = v4.4.9\n",
                "SHAKEMAP_RELEASE_TAG=v4.4.9-rc1\n",
                "SHAKEMAP_RELEASE_TAG=v4.4.9\nSHAKEMAP_RELEASE_TAG=v4.4.9\n",
                "OTHER_RELEASE_TAG=v1.0.0\n",
            ):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(
                    release.ReleaseResolutionError
                ):
                    release.load_declared_release_tag(path)

    def test_lightweight_and_annotated_tags_resolve(self) -> None:
        self.assertEqual(
            release.resolve_tag_commit_from_ls_remote(
                "v4.4.9", f"{COMMIT_A}\trefs/tags/v4.4.9\n"
            ),
            COMMIT_A,
        )
        annotated = (
            f"{COMMIT_A}\trefs/tags/v4.4.9\n"
            f"{COMMIT_B}\trefs/tags/v4.4.9^{{}}\n"
        )
        self.assertEqual(
            release.resolve_tag_commit_from_ls_remote("v4.4.9", annotated),
            COMMIT_B,
        )

    def test_declared_tag_resolves_only_against_official_repository(self) -> None:
        with patch.object(release, "query_official_tag", return_value=COMMIT_A) as query:
            resolved = release.resolve_declared_release(PROJECT_DIR / "VERSIONS.env")
        self.assertEqual(
            resolved,
            release.ResolvedRelease(tag="v4.4.9", commit=COMMIT_A),
        )
        query.assert_called_once_with("v4.4.9")

    def test_no_latest_release_discovery_or_tag_override_surface(self) -> None:
        for name in (
            "fetch_release_metadata",
            "select_latest_stable_release",
            "resolve_latest_official_release",
            "OFFICIAL_RELEASES_URL",
        ):
            self.assertFalse(hasattr(release, name), name)
        resolve_parser = release._parser()._subparsers._group_actions[0].choices["resolve"]
        options = {
            option
            for action in resolve_parser._actions
            for option in action.option_strings
        }
        self.assertEqual(options, {"-h", "--help", "--versions-file"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
