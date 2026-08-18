#!/usr/bin/env python3
"""Controlled host tests for candidate-image build and promotion safety."""
from __future__ import annotations

import fnmatch
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
BUILD_HELPER = PROJECT_DIR / "scripts/build-shakemap-docker.sh"
PRECEDING_ID = "sha256:" + "1" * 64
CANDIDATE_ID = "sha256:" + "2" * 64
SOURCE_COMMIT = "3" * 40


def dockerfile_copy_sources() -> list[str]:
    """Return source operands from the simple shell-form COPY instructions."""
    instructions: list[str] = []
    pending = ""
    for raw_line in (PROJECT_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instructions.append(pending)
        pending = ""
    if pending:
        instructions.append(pending)

    sources: list[str] = []
    for instruction in instructions:
        fields = shlex.split(instruction)
        if not fields or fields[0].upper() != "COPY":
            continue
        operands = fields[1:]
        while operands and operands[0].startswith("--"):
            operands.pop(0)
        if len(operands) < 2:
            raise AssertionError(f"malformed COPY instruction: {instruction}")
        sources.extend(operands[:-1])
    return sources


class ImageBuildTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="image_build_")
        self.root = Path(self.temporary.name)
        self.venv = self.root / "venv"
        self.bin = self.venv / "bin"
        self.bin.mkdir(parents=True)
        self.trace = self.root / "docker-trace.txt"
        self.tag_state = self.root / "canonical-tagged"
        self._write_executable(
            self.bin / "python",
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"-c\" ]]; then exit 0; fi\n"
            "if [[ \"$1\" == \"-m\" && \"$2\" == \"shakemap_service.release\" ]]; then\n"
            "  printf 'v4.4.9\\n%s\\nhttps://code.usgs.gov/ghsc/esi/shakemap.git\\n' \"$SOURCE_COMMIT\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 80\n",
        )
        self._write_executable(
            self.bin / "shake-in-docker",
            "#!/usr/bin/env bash\nexit 2\n",
        )
        self._write_executable(
            self.bin / "docker",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$DOCKER_TRACE\"\n"
            "if [[ \"$1 $2\" == \"image inspect\" ]]; then\n"
            "  ref=\"${@: -1}\"\n"
            "  if [[ \"$*\" == *\"RepoTags\"* ]]; then echo null; exit 0; fi\n"
            "  if [[ \"$ref\" == \"shakemap-docker:latest\" ]]; then\n"
            "    if [[ -e \"$TAG_STATE\" ]]; then echo \"$FAKE_CANDIDATE_ID\"; else echo \"$FAKE_PRECEDING_ID\"; fi\n"
            "  else echo \"$FAKE_CANDIDATE_ID\"; fi\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1\" == \"build\" ]]; then\n"
            "  if [[ \"${BUILD_FAIL:-no}\" == \"yes\" ]]; then exit 81; fi\n"
            "  echo \"$FAKE_CANDIDATE_ID\"; exit 0\n"
            "fi\n"
            "if [[ \"$1\" == \"run\" ]]; then\n"
            "  if [[ \"${VERIFY_FAIL:-no}\" == \"yes\" ]]; then exit 82; fi\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"image tag\" ]]; then touch \"$TAG_STATE\"; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"image rm\" ]]; then exit 0; fi\n"
            "exit 83\n",
        )
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": os.pathsep.join((str(self.bin), self.environment["PATH"])),
                "VIRTUAL_ENV": str(self.venv),
                "DOCKER_TRACE": str(self.trace),
                "TAG_STATE": str(self.tag_state),
                "FAKE_PRECEDING_ID": PRECEDING_ID,
                "FAKE_CANDIDATE_ID": CANDIDATE_ID,
                "SOURCE_COMMIT": SOURCE_COMMIT,
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run(self, **environment: str) -> subprocess.CompletedProcess[str]:
        selected = self.environment.copy()
        selected.update(environment)
        return subprocess.run(
            ["bash", str(BUILD_HELPER)],
            cwd=PROJECT_DIR,
            env=selected,
            capture_output=True,
            text=True,
        )

    def _trace(self) -> list[str]:
        return self.trace.read_text(encoding="utf-8").splitlines()

    def test_verified_untagged_candidate_is_promoted_by_identity(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = self._trace()
        build = next(line for line in trace if line.startswith("build "))
        self.assertNotIn("--platform", build)
        self.assertNotIn("--tag", build)
        self.assertNotIn(" -t ", f" {build} ")
        run = next(line for line in trace if line.startswith("run "))
        self.assertIn("--rm --network none", run)
        self.assertIn("--entrypoint /app/scripts/verify-shakemap-image.sh", run)
        self.assertTrue(run.endswith(CANDIDATE_ID), run)
        tag = next(line for line in trace if line.startswith("image tag "))
        self.assertEqual(tag, f"image tag {CANDIDATE_ID} shakemap-docker:latest")
        self.assertTrue(self.tag_state.exists())

    def test_failed_candidate_check_preserves_preceding_canonical_image(self) -> None:
        result = self._run(VERIFY_FAIL="yes")
        self.assertNotEqual(result.returncode, 0)
        trace = self._trace()
        self.assertFalse(any(line.startswith("image tag ") for line in trace), trace)
        self.assertIn(f"image rm {CANDIDATE_ID}", trace)
        self.assertFalse(self.tag_state.exists())
        self.assertEqual(
            trace[0],
            "image inspect --format {{.Id}} shakemap-docker:latest",
        )

    def test_failed_build_never_reaches_candidate_check_or_promotion(self) -> None:
        result = self._run(BUILD_FAIL="yes")
        self.assertNotEqual(result.returncode, 0)
        trace = self._trace()
        self.assertFalse(any(line.startswith("run ") for line in trace), trace)
        self.assertFalse(any(line.startswith("image tag ") for line in trace), trace)
        self.assertFalse(self.tag_state.exists())

    def test_release_and_image_overrides_are_not_accepted(self) -> None:
        for option in ("--release-tag", "--tag"):
            result = subprocess.run(
                ["bash", str(BUILD_HELPER), option, "unsupported"],
                cwd=PROJECT_DIR,
                env=self.environment,
                capture_output=True,
                text=True,
            )
            with self.subTest(option=option):
                self.assertEqual(result.returncode, 2)
                self.assertIn("Unknown option", result.stderr)
        self.assertFalse(self.trace.exists())

    def test_explicit_platform_is_passed_to_docker_build(self) -> None:
        for platform in ("linux/amd64", "linux/arm64"):
            self.trace.unlink(missing_ok=True)
            self.tag_state.unlink(missing_ok=True)
            result = subprocess.run(
                ["bash", str(BUILD_HELPER), "--platform", platform],
                cwd=PROJECT_DIR,
                env=self.environment,
                capture_output=True,
                text=True,
            )
            with self.subTest(platform=platform):
                self.assertEqual(result.returncode, 0, result.stderr)
                build = next(
                    line for line in self._trace() if line.startswith("build ")
                )
                build_arguments = shlex.split(build)
                self.assertEqual(build_arguments.count("--platform"), 1)
                platform_index = build_arguments.index("--platform")
                self.assertEqual(build_arguments[platform_index + 1], platform)

        help_result = subprocess.run(
            ["bash", str(BUILD_HELPER), "--help"],
            cwd=PROJECT_DIR,
            env=self.environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("default: Docker selects", help_result.stdout)


class DockerfileAssemblyTests(unittest.TestCase):
    def test_copy_sources_exist_outside_ignored_roots(self) -> None:
        patterns = [
            line.strip().strip("/")
            for line in (PROJECT_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertIn("tests", patterns)
        self.assertFalse(any(pattern.startswith("!") for pattern in patterns))

        sources = dockerfile_copy_sources()
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source):
                self.assertFalse(source.startswith("/"), source)
                self.assertTrue((PROJECT_DIR / source).exists(), source)
                parts = Path(source).parts
                matched_patterns = [
                    pattern
                    for pattern in patterns
                    if any(fnmatch.fnmatchcase(part, pattern) for part in parts)
                ]
                self.assertEqual(matched_patterns, [], source)
        self.assertTrue(
            any(
                source.startswith(
                    "verification/scenarios/v4.4.9/south-napa-global/"
                )
                for source in sources
            )
        )
        self.assertTrue(any(source.startswith("verification/packages/") for source in sources))

    def test_final_dependency_inventory_is_fail_closed(self) -> None:
        dockerfile = (PROJECT_DIR / "Dockerfile").read_text(encoding="utf-8")
        verifier = (PROJECT_DIR / "scripts/verify-shakemap-image.sh").read_text(
            encoding="utf-8"
        )
        install = dockerfile.index("pip install --no-cache-dir /app")
        dependency_check = dockerfile.index("python -m pip check", install)
        temporary = dockerfile.index("mktemp /opt/shakemap-build/dependencies.", dependency_check)
        freeze = dockerfile.index("python -m pip freeze --all", dependency_check)
        sort = dockerfile.index("LC_ALL=C sort", freeze)
        cleanup = dockerfile.index('rm "${dependencies_tmp}"', sort)
        identity = dockerfile.index("shakemap_service.build_identity write", cleanup)
        self.assertLess(install, dependency_check)
        self.assertLess(dependency_check, temporary)
        self.assertLess(temporary, freeze)
        self.assertLess(freeze, sort)
        self.assertLess(sort, cleanup)
        self.assertLess(cleanup, identity)
        freeze_line = next(
            line for line in dockerfile.splitlines() if "pip freeze --all" in line
        )
        self.assertNotIn("|", freeze_line)
        self.assertIn('"${dependencies_tmp}"', freeze_line)
        self.assertIn("check python -m pip check", verifier)


if __name__ == "__main__":
    unittest.main(verbosity=2)
