#!/usr/bin/env python3
"""Static host tests for the fixed operator command surface."""
from __future__ import annotations

import re
import fcntl
import hashlib
import json
import os
import signal
import shutil
import stat
import subprocess
import tarfile
import tempfile
import sys
import time
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


def remove_fixture_lock(daemon: str) -> None:
    """Remove only the idle lock for this test's unique fake daemon."""
    key = hashlib.sha256((daemon + "\0shakemap-docker").encode()).hexdigest()
    path = Path("/tmp") / f"shakemap-finalization-{key}.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.unlink()
    finally:
        os.close(descriptor)


class OperatorWorkflowTests(unittest.TestCase):
    def _run_deployment_verifier(
        self,
        failure: str | None,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runtime = root / "runtime"
        service = runtime / "shakemap"
        for relative in (
            "data/global",
            "data/regional",
            "data/test",
            "data/inputs",
            "products",
            "logs",
            ".service",
        ):
            service.joinpath(relative).mkdir(parents=True, exist_ok=True)
        fake_env = root / "venv/bin"
        fake_env.mkdir(parents=True)
        python_trace = root / "python-trace"
        python = fake_env / "python"
        python.write_text(
            """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PYTHON_TRACE"
[[ "$1 $2 $3" == "-m unittest discover" ]] && exit 0
[[ "$*" == *'shakemap_service.finalization fail'* ]] && exit 0
[[ "$1" == "-" && "$2" == http://* && "$SERVICE_FAIL" == 1 ]] && exit 45
exit 0
""",
            encoding="utf-8",
        )
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        cli = fake_env / "shake-in-docker"
        cli.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        fakebin = root / "bin"
        fakebin.mkdir()
        docker_trace = root / "docker-trace"
        self.addCleanup(remove_fixture_lock, str(docker_trace))
        docker = fakebin / "docker"
        docker.write_text(
            """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_TRACE"
if [[ "$1 $2" == "context show" ]]; then echo fixture-context; exit 0; fi
if [[ "$1" == info ]]; then echo "$DOCKER_TRACE"; exit 0; fi
if [[ "$1 $2" == "image inspect" ]]; then
  case "$*" in
    *'{{json .Config}}'*) echo '{"Env":[],"User":"sysop","Entrypoint":["/app/entrypoint.sh"],"Cmd":null}' ;;
    *'{{.Id}}'*) echo 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
    *'org.usgs.shakemap.release'*) echo 'v4.4.9' ;;
    *'org.usgs.shakemap.version'*) echo '4.4.9' ;;
    *'org.usgs.shakemap.commit'*) echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  esac
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  [[ "$*" == *'{{json .}}'* && "$OWNERSHIP_FAIL" == 1 ]] && exit 31
  [[ "$*" == *'{{json .}}'* ]] && { echo '{}'; exit 0; }
  [[ "$*" == *'State.Running'* ]] && { echo true; exit 0; }
  exit 0
fi
if [[ "$1" == "exec" && "$*" == *'shakemap_service.finalization fail'* ]]; then exit 0; fi
if [[ "$1" == "exec" && "$*" == *'verify-shakemap-image.sh'* && "$INTERNAL_FAIL" == 1 ]]; then exit 44; fi
exit 0
""",
            encoding="utf-8",
        )
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": os.pathsep.join((str(fake_env), str(fakebin), environment["PATH"])),
                "VIRTUAL_ENV": str(root / "venv"),
                    "REAL_PYTHON": sys.executable,
                "PYTHON_TRACE": str(python_trace),
                "DOCKER_TRACE": str(docker_trace),
                "INTERNAL_FAIL": "1" if failure == "internal" else "0",
                "SERVICE_FAIL": "1" if failure == "service" else "0",
                "OWNERSHIP_FAIL": "1" if failure == "ownership" else "0",
            }
        )
        result = subprocess.run(
            [
                "bash",
                str(PROJECT / "scripts/verify-shakemap-deployment.sh"),
                "--runtime-root",
                str(runtime),
            ],
            cwd=PROJECT,
            env=environment,
            capture_output=True,
            text=True,
        )
        return (
            result,
            docker_trace.read_text(encoding="utf-8").splitlines(),
            python_trace.read_text(encoding="utf-8").splitlines(),
        )

    def test_makefile_exposes_approved_thin_public_targets(self) -> None:
        source = (PROJECT / "Makefile").read_text(encoding="utf-8")
        targets = {
            match.group(1)
            for match in re.finditer(r"^([a-z][a-z-]*):\s*$", source, re.MULTILINE)
        }
        self.assertEqual(
            targets,
            {
                "build",
                "data",
                "fix-permissions",
                "finalize",
                "start",
                "stop",
                "verify",
                "check",
            },
        )
        recipes = [line.strip() for line in source.splitlines() if line.startswith("\t")]
        self.assertEqual(len(recipes), 8)
        self.assertTrue(all("$(SCRIPTS)/" in recipe for recipe in recipes))
        self.assertIn(
            '$(SCRIPTS)/fix-shakemap-permissions.sh --runtime-root "$(RUNTIME_ROOT)" $(PERMISSION_TARGET_OPTION)',
            recipes,
        )
        self.assertIn(
            "fix-permissions: export PERMISSION_TARGET_VALUE := $(value PERMISSION_TARGET)",
            source,
        )
        self.assertIn('--target "$${PERMISSION_TARGET_VALUE}"', source)

    def test_container_command_has_fixed_identity_and_exact_mount_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = root / "shakemap"
            for relative in (
                "data/global",
                "data/regional",
                "data/test",
                "data/inputs",
                "products",
                "logs",
                ".service",
            ):
                service.joinpath(relative).mkdir(parents=True, exist_ok=True)
            script = f"""
source scripts/container-configuration.sh
RUNTIME_ABS={root!s}
SERVICE_ABS={service!s}
PORT=19010
MAX_CONCURRENT=3
IMAGE_ID=sha256:{'a' * 64}
IMAGE_DIGEST=''
container_run_command isolated
printf '%s\n' "${{CONTAINER_COMMAND[*]}}"
container_run_command published
printf '%s\n' "${{CONTAINER_COMMAND[*]}}"
container_create_command isolated
printf '%s\n' "${{CONTAINER_COMMAND[*]}}"
"""
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=PROJECT,
                check=True,
                capture_output=True,
                text=True,
            )
        isolated, published, created = result.stdout.splitlines()
        for command in (isolated, published, created):
            self.assertIn("--name shakemap-docker", command)
            self.assertTrue(command.endswith(" shakemap-docker:latest"), command)
            self.assertIn(f"-v {root}:/home/sysop/runtime", command)
            self.assertIn("data/global:/home/sysop/runtime/shakemap/data/global:ro", command)
            self.assertIn("data/regional:/home/sysop/runtime/shakemap/data/regional:ro", command)
            self.assertIn("data/test:/home/sysop/runtime/shakemap/data/test:ro", command)
            self.assertNotIn("--env", command)
        self.assertIn("--network none", isolated)
        self.assertNotIn(" -p ", isolated)
        self.assertIn("-p 19010:9010", published)
        self.assertNotIn("--network none", published)
        self.assertTrue(created.startswith("docker create --name shakemap-docker"))
        self.assertIn("--network none", created)
        self.assertNotIn(" -d ", created)
        self.assertNotIn(" -p ", created)

    def test_finalization_retains_required_publication_and_ownership_gates(self) -> None:
        source = (PROJECT / "scripts/finalize-shakemap.sh").read_text(encoding="utf-8")
        ready = source.index("python -m shakemap_service.finalization ready")
        parity = source.index('CURRENT_STEP="pre-ready public parity checks"')
        self.assertGreater(ready, parity)
        self.assertIn("--file /opt/shakemap-verification/event.xml", source)
        self.assertIn("verify-shakemap-image.sh --deployment", source)
        self.assertLess(
            source.index('CURRENT_STEP="existing container mount inspection"'),
            source.index('CURRENT_STEP="graceful service stop"'),
        )
        self.assertLess(
            source.index("python -m shakemap_service.finalization arm"),
            source.index('docker start "${CANONICAL_CONTAINER}"'),
        )
        # Retain the unique failure-order guard until a behavior test injects
        # staging cleanup failure; ordinary stop/retain behavior is tested below.
        failure_handler = source[
            source.index("fail_closed() {") : source.index("trap fail_closed EXIT")
        ]
        self.assertLess(
            failure_handler.index("shakemap_service.finalization fail"),
            failure_handler.index("cleanup_seed_staging"),
        )
        self.assertIn("cleanup_seed_staging ||", failure_handler)
        deployment_verifier = (
            PROJECT / "scripts/verify-shakemap-deployment.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("verify-shakemap-image.sh --deployment", deployment_verifier)
        self.assertIn(
            '"verification" / "scenarios" / release / "south-napa-global"',
            deployment_verifier,
        )
        self.assertIn('"${PROJECT_CLI}" "${IMAGE_RELEASE}"', deployment_verifier)
        self.assertIn('str(scenario / "event.xml")', deployment_verifier)
        self.assertIn('str(scenario / "event_dat.xml")', deployment_verifier)

        self.assertIn("probe_writable_access", source)
        self.assertIn("repair-shakemap-writable-paths.sh", source)
        self.assertNotIn("chown -R", source)
        mode_commands = [
            line for line in source.splitlines()
            if "chmod " in line and not line.lstrip().startswith("#")
        ]
        self.assertTrue(all('"${SEED_STAGING}"' in line for line in mode_commands))

    def test_finalization_failure_records_reason_stops_and_retains_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            fake_env = root / "venv/bin"
            fake_env.mkdir(parents=True)
            python_trace = root / "python-trace"
            python = fake_env / "python"
            python.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PYTHON_TRACE"
if [[ "$1" == - && "${2:-}" == finalization-* ]]; then exec "$REAL_PYTHON" "$@"; fi
[[ "$*" == *'prepare-runtime'* ]] && exit 9
if [[ "${FAIL_BEGIN:-0}" == 1 && "$*" == *'finalization begin'* ]]; then
  echo 'ERROR: accepted calculations are unfinished; wait for them to finish' >&2
  exit 8
fi
exit 0
""",
                encoding="utf-8",
            )
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            cli = fake_env / "shake-in-docker"
            cli.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
            fakebin = root / "bin"
            fakebin.mkdir()
            docker_trace = root / "docker-trace"
            self.addCleanup(remove_fixture_lock, str(docker_trace))
            docker = fakebin / "docker"
            docker.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_TRACE"
if [[ "$1 $2" == "context show" ]]; then echo fixture-context; exit 0; fi
if [[ "$1" == info ]]; then echo "$DOCKER_TRACE"; exit 0; fi
if [[ "$1 $2" == "image inspect" ]]; then
  case "$*" in
    *'{{.Id}}'*) echo 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
    *'org.usgs.shakemap.release'*) echo 'v4.4.9' ;;
    *'org.usgs.shakemap.version'*) echo '4.4.9' ;;
    *'org.usgs.shakemap.commit'*) echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  esac
  exit 0
fi
if [[ "$1 $2" == "container ls" ]]; then echo shakemap-docker; exit 0; fi
if [[ "$1 $2" == "container inspect" ]]; then
  case "$*" in
    *'{{.State.Running}}'*) echo true ;;
    *'{{json .}}'*) "$REAL_PYTHON" -c 'import json,os; r=os.environ["RUNTIME"]; print(json.dumps(dict(Id="b"*64,Name="/shakemap-docker",Mounts=[dict(Type="bind",Source=r,Destination="/home/sysop/runtime",RW=True)]+[dict(Type="bind",Source=r+"/shakemap/data/"+n,Destination="/home/sysop/runtime/shakemap/data/"+n,RW=False) for n in ("global","regional","test")])))' ;;
  esac
  exit 0
fi
exit 0
""",
                encoding="utf-8",
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": os.pathsep.join((str(fake_env), str(fakebin), environment["PATH"])),
                    "VIRTUAL_ENV": str(root / "venv"),
                    "REAL_PYTHON": sys.executable,
                    "PYTHON_TRACE": str(python_trace),
                    "DOCKER_TRACE": str(docker_trace),
                    "RUNTIME": str(runtime.resolve()),
                }
            )
            result = subprocess.run(
                [
                    "bash",
                    str(PROJECT / "scripts/finalize-shakemap.sh"),
                    "--runtime-root",
                    str(runtime),
                ],
                cwd=PROJECT,
                env=environment,
                capture_output=True,
                text=True,
            )
            docker_calls = docker_trace.read_text(encoding="utf-8").splitlines()
            python_calls = python_trace.read_text(encoding="utf-8").splitlines()
            # Exercise the shell boundary too: a refused begin must keep its
            # specific reason without adding conditional permission escalation.
            environment['FAIL_BEGIN'] = '1'
            refused = subprocess.run(
                ['bash', str(PROJECT / 'scripts/finalize-shakemap.sh'),
                 '--runtime-root', str(runtime)],
                cwd=PROJECT, env=environment, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 9)
        self.assertNotIn('sudo', result.stderr)
        self.assertEqual(refused.returncode, 8)
        self.assertIn('accepted calculations are unfinished', refused.stderr)
        self.assertNotIn('sudo', refused.stderr)
        self.assertNotIn('permission-related', refused.stderr)
        self.assertTrue(any("finalization begin" in call for call in python_calls))
        self.assertTrue(any("prepare-runtime" in call for call in python_calls))
        self.assertTrue(any("finalization fail" in call for call in docker_calls))
        self.assertTrue(any(call.startswith("stop --time 65 " + "b" * 64) for call in docker_calls))
        self.assertFalse(any(call.startswith("rm ") for call in docker_calls))

    def test_deployment_verification_failure_revokes_stops_and_retains(self) -> None:
        for failure, code, step in (
            ("internal", 44, "container-internal checks"),
            ("service", 45, "running-service checks"),
        ):
            with self.subTest(failure=failure):
                result, docker_calls, _python_calls = self._run_deployment_verifier(failure)
                self.assertEqual(result.returncode, code, result.stderr)
                failure_calls = [
                    call
                    for call in docker_calls
                    if "shakemap_service.finalization fail" in call
                ]
                self.assertEqual(len(failure_calls), 1)
                self.assertIn(step, failure_calls[0])
                self.assertIn(f"exit code {code}", failure_calls[0])
                self.assertEqual(
                    [call for call in docker_calls if call.startswith("stop ")],
                    ["stop --time 65 shakemap-docker"],
                )
                self.assertFalse(any(call.startswith("rm ") for call in docker_calls))

    def test_deployment_verification_ownership_failure_and_success_do_not_revoke(self) -> None:
        refused, refused_docker, _ = self._run_deployment_verifier("ownership")
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(
            any("shakemap_service.finalization fail" in call for call in refused_docker)
        )
        self.assertFalse(any(call.startswith("stop ") for call in refused_docker))

        passed, passed_docker, _ = self._run_deployment_verifier(None)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertFalse(
            any("shakemap_service.finalization fail" in call for call in passed_docker)
        )
        self.assertFalse(any(call.startswith("stop ") for call in passed_docker))

    def test_finalization_extracts_selected_image_seeds_before_first_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "seeds.tar"
            with tarfile.open(archive, "w") as bundle:
                region = tarfile.TarInfo("region")
                region.type = tarfile.DIRTYPE
                region.mode = 0o555
                bundle.addfile(region)
            runtime = root / "runtime"
            service = runtime / "shakemap"
            for relative in (
                "data/global",
                "data/regional",
                "data/test",
                "data/inputs",
                "products",
                "logs",
                ".service/events",
                ".service/archive",
                ".service/queue",
            ):
                service.joinpath(relative).mkdir(parents=True, exist_ok=True)
            writable_directories = [
                service / relative
                for relative in (
                    "products",
                    "logs",
                    "data/inputs",
                    ".service/events",
                    ".service/archive",
                    ".service/queue",
                )
            ]
            original_modes = {}
            for index, directory in enumerate(writable_directories):
                directory.chmod((0o050, 0o150, 0o1050)[index % 3])
                original_modes[directory] = stat.S_IMODE(directory.stat().st_mode)
            fake_env = root / "venv/bin"
            fake_env.mkdir(parents=True)
            python_trace = root / "python-trace"
            python = fake_env / "python"
            python.write_text(
                """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$PYTHON_TRACE"
if [[ "$1" == - && "${2:-}" == finalization-* ]]; then exec "$REAL_PYTHON" "$@"; fi
exit 0
""",
                encoding="utf-8",
            )
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            cli = fake_env / "shake-in-docker"
            cli.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
            fakebin = root / "bin"
            fakebin.mkdir()
            docker_trace = root / "docker-trace"
            self.addCleanup(remove_fixture_lock, str(docker_trace))
            container_state = root / "container-state"
            docker = fakebin / "docker"
            docker.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_TRACE"
if [[ "$1 $2" == "context show" ]]; then echo fixture-context; exit 0; fi
if [[ "$1" == info ]]; then echo "$DOCKER_TRACE"; exit 0; fi
if [[ "$1 $2" == "image inspect" ]]; then
  case "$*" in
    *'{{.Id}}'*) echo 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
    *'org.usgs.shakemap.release'*) echo 'v4.4.9' ;;
    *'org.usgs.shakemap.version'*) echo '4.4.9' ;;
    *'org.usgs.shakemap.commit'*) echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  esac
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  [[ -e "$CONTAINER_STATE" ]] || exit 1
  [[ "$*" == *'State.Status'* ]] && echo 'exited:0:'
  [[ "$*" == *'State.Running'* ]] && echo false
  exit 0
fi
if [[ "$1 $2" == "container ls" ]]; then
  [[ ! -e "$CONTAINER_STATE" ]] || echo shakemap-docker
  exit 0
fi
if [[ "$1" == "create" ]]; then touch "$CONTAINER_STATE"; echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; exit 0; fi
if [[ "$1" == "cp" ]]; then cat "$SEED_ARCHIVE"; exit 0; fi
if [[ "$1 $2" == "start -a" ]]; then exit 0; fi
if [[ "$1" == "start" ]]; then exit 17; fi
exit 0
""",
                encoding="utf-8",
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            chown = fakebin / "chown"
            chown.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            chown.chmod(chown.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": os.pathsep.join((str(fake_env), str(fakebin), environment["PATH"])),
                    "VIRTUAL_ENV": str(root / "venv"),
                    "REAL_PYTHON": sys.executable,
                    "PYTHON_TRACE": str(python_trace),
                    "DOCKER_TRACE": str(docker_trace),
                    "CONTAINER_STATE": str(container_state),
                    "SEED_ARCHIVE": str(archive),
                }
            )
            result = subprocess.run(
                [
                    "bash",
                    str(PROJECT / "scripts/finalize-shakemap.sh"),
                    "--runtime-root",
                    str(runtime),
                ],
                cwd=PROJECT,
                env=environment,
                capture_output=True,
                text=True,
            )
            docker_calls = docker_trace.read_text(encoding="utf-8").splitlines()
            python_calls = python_trace.read_text(encoding="utf-8").splitlines()
            prepared_modes = {
                directory: stat.S_IMODE(directory.stat().st_mode)
                for directory in writable_directories
            }

        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(
            prepared_modes,
            original_modes,
        )
        create = next(index for index, call in enumerate(docker_calls) if call.startswith("create "))
        copy = next(index for index, call in enumerate(docker_calls) if call.startswith("cp "))
        start = next(index for index, call in enumerate(docker_calls) if call.startswith("start shakemap-docker"))
        self.assertLess(create, copy)
        self.assertLess(copy, start)
        self.assertIn("--network none", docker_calls[create])
        self.assertNotIn(" -p ", docker_calls[create])
        self.assertIn(
            "shakemap-docker:/opt/shakemap-seeds/regional/.",
            docker_calls[copy],
        )
        seeded = [call for call in python_calls if "prepare-runtime --regional-seeds" in call]
        self.assertEqual(len(seeded), 1)
        self.assertNotIn(str(PROJECT / "regional-configs"), seeded[0])
        staged = Path(seeded[0].split("--regional-seeds ", 1)[1])
        self.assertFalse(staged.exists())


class FinalizerSafetyTests(unittest.TestCase):
    """Run the real host lifecycle boundary with Docker replaced by fixtures."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="finalizer safety ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.addCleanup(remove_fixture_lock, str(self.root))
        self.runtime = self.root / "selected runtime"
        self.bin = self.root / "bin"
        self.venv = self.root / "venv"
        self.bin.mkdir()
        (self.venv / "bin").mkdir(parents=True)
        self.env = dict(os.environ, PATH=f"{self.venv / 'bin'}:{self.bin}:{os.environ['PATH']}",
                        VIRTUAL_ENV=str(self.venv), REAL_PYTHON=sys.executable,
                        SAFETY_FIXTURE=str(self.root), DOCKER_CONTEXT="fixture-alias-a")
        self.env.pop("RUNTIME_ROOT", None)
        self.helper = PROJECT / "scripts/finalize-shakemap.sh"
        self.executable(self.venv / "bin/python", r'''#!/usr/bin/env bash
if [[ "$1 $2 $3" == '-m shakemap_service.finalization prepare-runtime' ]]; then
    touch "$SAFETY_FIXTURE/preparing"
    if [[ "${NESTED_HELPER:-}" != '' ]]; then
        bash "$NESTED_HELPER" --runtime-root "$SAFETY_FIXTURE/nested-runtime" > "$SAFETY_FIXTURE/nested-output" 2>&1
        echo "$?" > "$SAFETY_FIXTURE/nested-result"
    fi
    if [[ "${PAUSE_PREPARATION:-}" == 1 ]]; then
        exec "$REAL_PYTHON" - <<'PY'
import os, time
from pathlib import Path
while not Path(os.environ['SAFETY_FIXTURE'], 'release').exists():
    time.sleep(.02)
raise SystemExit(29)
PY
    fi
    exit 29
fi
exec "$REAL_PYTHON" "$@"
''')
        self.executable(self.venv / "bin/shake-in-docker", "#!/usr/bin/env bash\nexit 0\n")
        self.executable(self.bin / "docker", f"#!{sys.executable}\n" + r'''
import json, os, sys
from pathlib import Path
root = Path(os.environ['SAFETY_FIXTURE'])
a = sys.argv[1:]
with (root / 'docker-calls').open('a') as trace:
    trace.write(json.dumps(a) + '\n')
current = root / 'canonical.json'
if a[:2] == ['context', 'show']:
    print('fixture-default')
elif a[0] == 'info':
    print(root)
elif a[:2] == ['container', 'ls']:
    if os.environ.get('FAIL_PRESENCE'): raise SystemExit(37)
    if current.exists(): print('shakemap-docker')
elif a[:2] == ['container', 'inspect']:
    if os.environ.get('FAIL_INSPECTION'): raise SystemExit(38)
    target = current if a[-1] == 'shakemap-docker' else root / (a[-1] + '.json')
    if not target.exists(): raise SystemExit(1)
    data = json.loads(target.read_text())
    if '{{json .}}' in a: print(json.dumps(data))
    elif '{{.State.Running}}' in a: print(str(data['State']['Running']).lower())
elif a[:2] == ['image', 'inspect']:
    fmt = a[3]
    print('sha256:' + 'a' * 64 if '.Id' in fmt else 'v4.5.0' if 'release' in fmt
          else '4.5.0' if 'version' in fmt else 'a' * 40 if 'commit' in fmt else '')
elif a[0] == 'exec':
    target = root / (a[1] + '.json')
    if not target.exists(): raise SystemExit(1)
    data = json.loads(target.read_text())
    data['failure_recorded'] = True
    target.write_text(json.dumps(data))
elif a[0] == 'stop':
    target = root / (a[-1] + '.json')
    if not target.exists(): raise SystemExit(1)
    data = json.loads(target.read_text())
    data['State']['Running'] = False
    target.write_text(json.dumps(data))
''')

    @staticmethod
    def executable(path: Path, source: str) -> None:
        path.write_text(source)
        path.chmod(0o755)

    def container(self, runtime: Path, *, identifier: str = "b", running: bool = True) -> dict:
        data = dict(Id=identifier * 64, Name="/shakemap-docker", Image="sha256:" + "c" * 64,
                    Config=dict(User="old-user", Entrypoint=["old-entrypoint"], Cmd=["old-command"]),
                    State=dict(Running=running), Mounts=[dict(Type="bind", Source=str(runtime),
                    Destination="/home/sysop/runtime", RW=True)] + [dict(Type="bind",
                    Source=str(runtime / "shakemap/data" / name),
                    Destination=f"/home/sysop/runtime/shakemap/data/{name}", RW=False)
                    for name in ("global", "regional", "test")])
        self.publish_container(data)
        return data

    def publish_container(self, data: dict) -> None:
        source = json.dumps(data)
        (self.root / "canonical.json").write_text(source)
        (self.root / (data["Id"] + ".json")).write_text(source)

    def command(self, runtime: Path | None = None, *, helper: Path | None = None) -> list[str]:
        return ["/bin/bash", str(helper or self.helper), "--runtime-root", str(runtime or self.runtime)]

    def run_finalizer(self, runtime: Path | None = None, **options) -> subprocess.CompletedProcess:
        return subprocess.run(self.command(runtime), env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=15, **options)

    def calls(self) -> list[list[str]]:
        path = self.root / "docker-calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_preparation(self, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (self.root / "preparing").exists():
                return
            if process.poll() is not None:
                self.fail(f"finalizer exited early: {process.communicate()}")
            time.sleep(.02)
        self.fail("finalizer did not reach preparation")

    def copied_helper(self) -> Path:
        scripts = self.root / "another checkout/scripts"
        scripts.mkdir(parents=True)
        for name in ("container-configuration.sh", "finalize-shakemap.sh"):
            shutil.copyfile(PROJECT / "scripts" / name, scripts / name)
        return scripts / "finalize-shakemap.sh"

    def test_foreign_mounts_preserve_container_and_both_readiness_records(self) -> None:
        actual = self.root / "actual runtime"
        for runtime in (self.runtime, actual):
            record = runtime / "shakemap/.service/readiness.json"
            record.parent.mkdir(parents=True)
            record.write_bytes(b"existing readiness must stay byte-identical")
        data = self.container(actual)
        before = (self.root / "canonical.json").read_bytes()
        result = self.run_finalizer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("To switch runtimes deliberately", result.stderr)
        self.assertEqual((self.root / "canonical.json").read_bytes(), before)
        self.assertEqual(json.loads((self.root / (data['Id'] + '.json')).read_text()), data)
        for runtime in (self.runtime, actual):
            self.assertEqual((runtime / "shakemap/.service/readiness.json").read_bytes(),
                             b"existing readiness must stay byte-identical")
        self.assertFalse(any(call[0] in {"exec", "stop", "rm", "start", "create", "run"} for call in self.calls()))
        missing = self.root / "not-created-runtime"
        result = self.run_finalizer(missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())

    def test_mount_shapes_and_inspection_errors_fail_before_runtime_creation(self) -> None:
        for alteration in ("missing-overlay", "foreign-overlay", "writable-overlay", "extra-mount",
                           "FAIL_PRESENCE", "FAIL_INSPECTION"):
            with self.subTest(alteration=alteration):
                data = self.container(self.runtime)
                if alteration == "missing-overlay": data['Mounts'].pop()
                if alteration == "foreign-overlay": data['Mounts'][1]['Source'] = str(self.root / 'foreign')
                if alteration == "writable-overlay": data['Mounts'][1]['RW'] = True
                if alteration == "extra-mount": data['Mounts'].append(dict(Type='bind', Source='/other', Destination='/other', RW=True))
                self.publish_container(data)
                environment = dict(self.env)
                if alteration.startswith("FAIL_"): environment[alteration] = "1"
                result = subprocess.run(self.command(), cwd=self.root, env=environment,
                                        capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.runtime.exists())
        self.assertFalse(any(call[0] in {"exec", "stop", "rm", "start", "create", "run"} for call in self.calls()))

    def test_same_runtime_old_image_and_stopped_probe_are_refinalizable(self) -> None:
        for probe in (False, True):
            with self.subTest(probe=probe):
                data = self.container(self.runtime, running=not probe)
                if probe:
                    data['Config'].update(User='1000:1000', Entrypoint=['python'], Cmd=['-c', 'old probe'])
                    self.publish_container(data)
                result = self.run_finalizer()
                self.assertEqual(result.returncode, 29, result.stderr)
                self.assertTrue((self.root / "preparing").exists())
                self.assertNotIn("mounts do not belong", result.stderr)

    def test_first_install_external_cwd_flags_and_no_argument_defaults(self) -> None:
        relative = self.root / "relative runtime"
        result = subprocess.run(["/bin/bash", str(self.helper), "--runtime-root", relative.name,
                                 "--port", "19017", "--max-concurrent", "3"],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 29, result.stderr)
        self.assertTrue((relative / "shakemap/.service/readiness.json").is_file())
        copied = self.copied_helper()
        result = subprocess.run(["/bin/bash", str(copied)], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 29, result.stderr)
        self.assertTrue((copied.parent.parent / "runtime/shakemap/.service/readiness.json").is_file())
        # Help stays usable without an active project environment or Docker.
        result = subprocess.run(["/bin/bash", str(self.helper), "--help"], cwd=self.root,
                                env=dict(PATH="/usr/bin:/bin"), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failure_cleanup_uses_owned_id_after_name_replacement(self) -> None:
        owned = self.container(self.runtime)
        env = dict(self.env, PAUSE_PREPARATION="1")
        process = subprocess.Popen(self.command(), cwd=self.root, env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        self.wait_preparation(process)
        replacement = self.container(self.root / "another runtime", identifier="d")
        (self.root / "release").touch()
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 29, stderr)
        cleanup = [call for call in self.calls() if call[0] in {"exec", "stop"}]
        self.assertTrue(cleanup)
        self.assertTrue(all(call[-1] == owned['Id'] if call[0] == 'stop' else call[1] == owned['Id'] for call in cleanup))
        self.assertEqual(json.loads((self.root / (replacement['Id'] + '.json')).read_text()), replacement)
        self.assertEqual(json.loads((self.root / "canonical.json").read_text()), replacement)

    def test_lock_overlaps_runtimes_checkouts_context_aliases_and_recovers(self) -> None:
        copied = self.copied_helper()
        process = subprocess.Popen(self.command(), cwd=self.root,
                                   env=dict(self.env, PAUSE_PREPARATION="1", NESTED_HELPER=str(copied)),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        self.wait_preparation(process)
        second_runtime = self.root / "second runtime"
        second_env = dict(self.env, DOCKER_CONTEXT="fixture-alias-b")
        refused = subprocess.run(self.command(second_runtime, helper=copied), cwd=self.root,
                                 env=second_env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("another finalization is already running", refused.stderr)
        self.assertFalse(second_runtime.exists())
        deadline = time.monotonic() + 5
        while not (self.root / "nested-result").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertNotEqual((self.root / "nested-result").read_text().strip(), "0")
        self.assertIn("another finalization is already running", (self.root / "nested-output").read_text())
        # An ordinary interrupted attempt releases the kernel lock; no stale
        # marker cleanup or special flag is needed for another installation.
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=10)
        retried = subprocess.run(self.command(second_runtime, helper=copied), cwd=self.root,
                                 env=second_env, capture_output=True, text=True, timeout=10)
        self.assertEqual(retried.returncode, 29, retried.stderr)
        self.assertTrue(second_runtime.exists())

    def test_killed_owner_cannot_release_lock_while_its_child_is_still_working(self) -> None:
        process = subprocess.Popen(self.command(), cwd=self.root,
                                   env=dict(self.env, PAUSE_PREPARATION="1"),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        self.addCleanup(lambda: self._kill_process_group(process.pid))
        self.wait_preparation(process)
        # Kill only the owning Bash. Its in-progress preparation child still
        # owns an inherited descriptor until that operation actually exits.
        process.kill()
        process.wait(timeout=5)
        refused = self.run_finalizer(self.root / "second-runtime")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("another finalization is already running", refused.stderr)
        (self.root / "release").touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            retried = self.run_finalizer(self.root / "second-runtime")
            if retried.returncode == 29:
                break
            self.assertIn("another finalization is already running", retried.stderr)
            time.sleep(.02)
        self.assertEqual(retried.returncode, 29, retried.stderr)

    @staticmethod
    def _kill_process_group(identifier: int) -> None:
        try:
            os.killpg(identifier, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
