#!/usr/bin/env python3
"""Controlled host tests for candidate-image build and promotion safety."""
from __future__ import annotations

import os
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
