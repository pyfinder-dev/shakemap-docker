"""Real host archive extraction and missing-only seed publication fixtures."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class SeedExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="seed extraction ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime"
        self.regional = self.runtime / "shakemap/data/regional"
        self.existing = self.regional / "existing"
        self.existing.mkdir(parents=True)
        self.existing.joinpath("model.conf").write_bytes(b"operator configuration")
        self.existing.joinpath("model.conf").chmod(0o640)
        self.existing.chmod(0o750)
        self.staging_parent = self.root / "temporary staging"
        self.staging_parent.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.archive = self.root / "seeds.tar"
        self.real_tar = shutil.which("tar")
        self.assertIsNotNone(self.real_tar)
        self.environment = dict(
            os.environ,
            PATH=f"{self.bin}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            TMPDIR=str(self.staging_parent),
            SEED_ARCHIVE=str(self.archive),
            DOCKER_TRACE=str(self.root / "docker-trace"),
            REAL_TAR=self.real_tar,
        )
        self.executable("docker", f"#!{sys.executable}\n" + """
import json, os, shutil, sys
with open(os.environ['DOCKER_TRACE'], 'a') as trace:
    trace.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] != 'cp':
    raise SystemExit(1)
with open(os.environ['SEED_ARCHIVE'], 'rb') as source:
    shutil.copyfileobj(source, sys.stdout.buffer)
sys.stdout.buffer.flush()
if os.environ.get('PRODUCER_FAIL'):
    print('injected archive producer failure', file=sys.stderr)
    raise SystemExit(45)
""")

    def executable(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def make_archive(self, unsafe: str | None = None) -> None:
        with tarfile.open(self.archive, "w") as archive:
            for name in (".", "existing", "new-region", "new-region/nested"):
                item = tarfile.TarInfo(name)
                item.type = tarfile.DIRTYPE
                item.mode = 0o555
                archive.addfile(item)
            for name in ("existing/model.conf", "new-region/nested/model.conf"):
                payload = b"immutable image configuration"
                item = tarfile.TarInfo(name)
                item.size = len(payload)
                item.mode = 0o444
                archive.addfile(item, io.BytesIO(payload))
            if unsafe is not None:
                item = tarfile.TarInfo("new-region/unsafe")
                item.type = tarfile.SYMTYPE if unsafe == "symlink" else tarfile.LNKTYPE
                item.linkname = (
                    str(self.existing / "model.conf")
                    if unsafe == "symlink" else "new-region/nested/model.conf"
                )
                archive.addfile(item)

    def run_extraction(self) -> subprocess.CompletedProcess[str]:
        source = (PROJECT / "scripts/finalize-shakemap.sh").read_text()
        # Execute the production cleanup/trap/extraction code verbatim. Only the
        # Docker archive producer is replaced; tar and Python publication are real.
        cleanup = source.split("cleanup_seed_staging() {", 1)[1].split("\nwhile [[ $#", 1)[0]
        failure = source.split("fail_closed() {", 1)[1].split("\nrequire_project_environment", 1)[0]
        extraction = source.split('CURRENT_STEP="image regional seed extraction"', 1)[1].split(
            '\nRUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \\\n    python -m shakemap_service.finalization arm', 1
        )[0]
        script = "\n".join([
            "set -euo pipefail", "BEGUN=1", "COMMITTED=0", 'SEED_STAGING=""',
            'CANONICAL_CONTAINER="shakemap-docker"',
            f"RUNTIME_ABS={shlex.quote(str(self.runtime))}",
            f"SERVICE_ABS={shlex.quote(str(self.runtime / 'shakemap'))}",
            'CURRENT_STEP="image regional seed extraction"',
            "cleanup_seed_staging() {" + cleanup,
            "fail_closed() {" + failure,
            extraction,
            "COMMITTED=1",
        ])
        return subprocess.run(
            ["bash", "-c", script], cwd=PROJECT, env=self.environment,
            capture_output=True, text=True,
        )

    def assert_existing_preserved(self) -> None:
        self.assertEqual(self.existing.joinpath("model.conf").read_bytes(), b"operator configuration")
        self.assertEqual(stat.S_IMODE(self.existing.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(self.existing.joinpath("model.conf").stat().st_mode), 0o640)
        self.assertEqual(list(self.staging_parent.iterdir()), [])

    def test_readonly_archive_publishes_only_missing_editable_configuration(self) -> None:
        self.make_archive()
        result = self.run_extraction()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"seeded": ["new-region"]})
        copied = self.regional / "new-region/nested/model.conf"
        self.assertEqual(copied.read_bytes(), b"immutable image configuration")
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o644)
        for directory in (copied.parent, copied.parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        copied.write_bytes(b"operator edited new copy")
        self.assert_existing_preserved()
        calls = [json.loads(line) for line in (self.root / "docker-trace").read_text().splitlines()]
        self.assertEqual(calls, [["cp", "shakemap-docker:/opt/shakemap-seeds/regional/.", "-"]])
        with tarfile.open(self.archive) as archive:
            self.assertTrue(all(item.mode == (0o555 if item.isdir() else 0o444) for item in archive))

    def test_producer_failure_does_not_publish_and_cleans_readonly_extraction(self) -> None:
        self.make_archive()
        self.environment["PRODUCER_FAIL"] = "1"
        result = self.run_extraction()
        self.assertEqual(result.returncode, 45, result.stderr)
        self.assertIn("injected archive producer failure", result.stderr)
        self.assertFalse(self.regional.joinpath("new-region").exists())
        self.assert_existing_preserved()
        readiness = json.loads((self.runtime / "shakemap/.service/readiness.json").read_text())
        self.assertEqual(readiness["state"], "not_ready")
        self.assertIn("exit code 45", readiness["reason"])

    def test_extractor_failure_keeps_status_and_cleans_readonly_partial_tree(self) -> None:
        self.make_archive()
        self.executable("tar", """#!/usr/bin/env bash
"$REAL_TAR" "$@" || exit $?
echo 'injected archive extractor failure' >&2
exit 46
""")
        result = self.run_extraction()
        self.assertEqual(result.returncode, 46, result.stderr)
        self.assertIn("injected archive extractor failure", result.stderr)
        self.assertFalse(self.regional.joinpath("new-region").exists())
        self.assert_existing_preserved()

    def test_truncated_archive_cleans_partial_extraction_without_publication(self) -> None:
        self.make_archive()
        # Four directory headers plus a file header, with its payload missing.
        self.archive.write_bytes(self.archive.read_bytes()[:5 * 512])
        result = self.run_extraction()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.regional.joinpath("new-region").exists())
        self.assert_existing_preserved()

    def test_unsafe_links_are_refused_without_changing_external_file(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                self.make_archive(kind)
                result = self.run_extraction()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unsafe regional seed entries", result.stderr)
                self.assertFalse(self.regional.joinpath("new-region").exists())
                self.assert_existing_preserved()


if __name__ == "__main__":
    unittest.main()
