#!/usr/bin/env python3
"""Deterministic host tests for image defaults and container startup safety."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = PROJECT_DIR / "scripts" / "build-shakemap-docker.sh"
START_SCRIPT = PROJECT_DIR / "scripts" / "start-shakemap-docker.sh"


class ContainerStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="container_startup_")
        self.root = Path(self.tempdir.name)
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        self.trace = self.root / "docker-trace.txt"
        self._write_executable(
            self.fakebin / "docker",
            """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_TRACE"
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$*" == *"{{.Id}}"* ]]; then
    echo 'sha256:1111111111111111111111111111111111111111111111111111111111111111'
  else
    echo 'shakemap-docker@sha256:2222222222222222222222222222222222222222222222222222222222222222'
  fi
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  if [[ "$3" == "-f" ]]; then
    [[ "$DOCKER_STATE" == "running" ]] && echo true || echo false
    exit 0
  fi
  [[ "$DOCKER_STATE" == "absent" ]] && exit 1 || exit 0
fi
if [[ "$1" == "run" ]]; then
  echo fake-container-id
  exit 0
fi
if [[ "$1" == "rm" ]]; then
  exit 88
fi
exit 0
""",
        )
        self.environment = os.environ.copy()
        self.environment["PATH"] = os.pathsep.join(
            (str(self.fakebin), self.environment["PATH"])
        )
        self.environment["DOCKER_TRACE"] = str(self.trace)
        self.environment["DOCKER_STATE"] = "absent"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _start(self, state: str) -> subprocess.CompletedProcess[str]:
        environment = self.environment.copy()
        environment["DOCKER_STATE"] = state
        return subprocess.run(
            [
                "bash",
                str(START_SCRIPT),
                "--runtime",
                str(self.root / f"runtime-{state}"),
                "--port",
                "19019",
            ],
            cwd=PROJECT_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )

    def _trace_lines(self) -> list[str]:
        if not self.trace.exists():
            return []
        return self.trace.read_text(encoding="utf-8").splitlines()

    def test_default_build_targets_stable_image_name(self) -> None:
        fake_python = self.fakebin / "release-command"
        self._write_executable(
            fake_python,
            """#!/usr/bin/env bash
if [[ "$3" == "resolve" ]]; then
  printf '%s\n' v4.4.10 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa https://code.usgs.gov/ghsc/esi/shakemap.git
  exit 0
fi
if [[ "$3" == "build-command" ]]; then
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--image-tag" ]]; then image_tag="$2"; break; fi
    shift
  done
  printf '%s\n' docker build --tag "$image_tag" .
  exit 0
fi
exit 1
""",
        )
        environment = self.environment.copy()
        environment["SHAKEMAP_BUILD_PYTHON"] = str(fake_python)
        result = subprocess.run(
            ["bash", str(BUILD_SCRIPT)],
            cwd=PROJECT_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any(
                line.startswith("build --tag shakemap-docker:latest ")
                for line in self._trace_lines()
            )
        )

    def test_default_start_uses_stable_container_and_image(self) -> None:
        result = self._start("absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        run = next(line for line in self._trace_lines() if line.startswith("run "))
        self.assertIn("--name shakemap-docker", run)
        self.assertTrue(run.endswith(" shakemap-docker:latest"), run)
        self.assertTrue(
            any(line.startswith("container inspect shakemap-docker") for line in self._trace_lines())
        )

    def test_help_text_and_makefile_agree_with_defaults(self) -> None:
        build_help = subprocess.run(
            ["bash", str(BUILD_SCRIPT), "--help"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        start_help = subprocess.run(
            ["bash", str(START_SCRIPT), "--help"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        makefile = (PROJECT_DIR / "Makefile").read_text(encoding="utf-8")
        self.assertIn("default: shakemap-docker:latest", build_help)
        self.assertIn("default: shakemap-docker", start_help)
        self.assertIn("default: shakemap-docker:latest", start_help)
        self.assertIn("IMAGE ?= shakemap-docker:latest", makefile)
        self.assertIn("CONTAINER ?= shakemap-docker", makefile)
        self.assertIn("RUNTIME ?= ./runtime", makefile)
        self.assertIn("PORT ?= 9010", makefile)

    def test_existing_running_container_is_preserved(self) -> None:
        result = self._start("running")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already running; it was not modified", result.stderr)
        self.assertIn("docker stop shakemap-docker", result.stderr)
        trace = self._trace_lines()
        self.assertFalse(any(line.startswith("rm ") for line in trace), trace)
        self.assertFalse(any(line.startswith("run ") for line in trace), trace)

    def test_existing_stopped_container_is_preserved(self) -> None:
        result = self._start("stopped")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists but is stopped; it was not modified", result.stderr)
        self.assertIn("docker start shakemap-docker", result.stderr)
        self.assertIn("docker rm shakemap-docker", result.stderr)
        trace = self._trace_lines()
        self.assertFalse(any(line.startswith("rm ") for line in trace), trace)
        self.assertFalse(any(line.startswith("run ") for line in trace), trace)


if __name__ == "__main__":
    unittest.main(verbosity=2)
