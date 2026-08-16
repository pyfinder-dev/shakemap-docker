#!/usr/bin/env python3
"""Host tests for package metadata and the REST-client boundary."""
from __future__ import annotations

import contextlib
import io
import tomllib
import unittest
from pathlib import Path

from shakemap_service import cli


PROJECT_DIR = Path(__file__).resolve().parent.parent


class ProjectPackagingTests(unittest.TestCase):
    def test_standard_metadata_declares_console_entry_point(self) -> None:
        metadata = tomllib.loads((PROJECT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["build-system"]["build-backend"], "setuptools.build_meta")
        self.assertEqual(metadata["project"]["name"], "shakemap-docker-service")
        self.assertEqual(
            metadata["project"]["scripts"]["shake-in-docker"],
            "shakemap_service.cli:main",
        )
        self.assertIn("data/*.json", metadata["tool"]["setuptools"]["package-data"]["shakemap_service"])

    def test_command_has_no_direct_runtime_or_native_access(self) -> None:
        source = (PROJECT_DIR / "shakemap_service/cli.py").read_text(encoding="utf-8")
        for forbidden in (
            "subprocess",
            "docker run",
            "from .runner",
            "from .paths",
            "from .status",
        ):
            self.assertNotIn(forbidden, source)

    def test_command_help_describes_all_public_operations(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(SystemExit, "0"):
            cli.main(["--help"])
        self.assertIn(
            "{health,config,configurations,submit,list,queue,status,products}",
            output.getvalue(),
        )
        self.assertIn("--url", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
