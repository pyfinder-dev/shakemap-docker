#!/usr/bin/env python3
"""Static host tests for the fixed operator command surface."""
from __future__ import annotations

import re
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


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
        docker = fakebin / "docker"
        docker.write_text(
            """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_TRACE"
if [[ "$1 $2" == "image inspect" ]]; then
  case "$*" in
    *'{{json .Config.Env}}'*) echo '[]' ;;
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

    def _run_finalization_permission_failure(
        self,
        failure: str,
    ) -> tuple[
        subprocess.CompletedProcess[str],
        dict[str, int],
        dict[str, int],
        list[str],
        list[str],
    ]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            writable = (
                "products",
                "logs",
                "data/inputs",
                ".service/events",
                ".service/archive",
                ".service/queue",
            )
            original_modes = {}
            for index, relative in enumerate(writable):
                directory = service / relative
                directory.chmod(0o1700 | (0o050, 0o025, 0o004)[index % 3])
                original_modes[relative] = stat.S_IMODE(directory.stat().st_mode)

            fake_env = root / "venv/bin"
            fake_env.mkdir(parents=True)
            python_trace = root / "python-trace"
            python = fake_env / "python"
            python.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$PYTHON_TRACE\"\nexit 0\n",
                encoding="utf-8",
            )
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            cli = fake_env / "shake-in-docker"
            cli.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR)

            fakebin = root / "bin"
            fakebin.mkdir()
            docker_trace = root / "docker-trace"
            docker = fakebin / "docker"
            docker.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_TRACE"
if [[ "$1 $2" == "image inspect" ]]; then
  case "$*" in
    *'{{.Id}}'*) echo 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
    *'org.usgs.shakemap.release'*) echo 'v4.4.9' ;;
    *'org.usgs.shakemap.version'*) echo '4.4.9' ;;
    *'org.usgs.shakemap.commit'*) echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  esac
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then exit 1; fi
exit 0
""",
                encoding="utf-8",
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

            chown_trace = root / "chown-trace"
            chown = fakebin / "chown"
            chown.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CHOWN_TRACE"
case "$3" in
  "$SERVICE_ROOT/products") /bin/chmod -t "$SERVICE_ROOT/products" ;;
  "$SERVICE_ROOT/logs") /bin/chmod -t "$SERVICE_ROOT/logs" ;;
  "$SERVICE_ROOT/data/inputs") /bin/chmod -t "$SERVICE_ROOT/data/inputs" ;;
  "$SERVICE_ROOT/.service")
    /bin/chmod -t "$SERVICE_ROOT/.service/events"
    /bin/chmod -t "$SERVICE_ROOT/.service/archive"
    /bin/chmod -t "$SERVICE_ROOT/.service/queue"
    ;;
esac
if [[ "$PERMISSION_FAILURE" == "chown" && "$3" == "$SERVICE_ROOT/.service" ]]; then
  exit 37
fi
exit 0
""",
                encoding="utf-8",
            )
            chown.chmod(chown.stat().st_mode | stat.S_IXUSR)

            chmod_trace = root / "chmod-trace"
            chmod_count = root / "chmod-count"
            chmod = fakebin / "chmod"
            chmod.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CHMOD_TRACE"
if [[ "$1" == "u+rwx" ]]; then
  count=0
  [[ ! -f "$CHMOD_COUNT" ]] || read -r count < "$CHMOD_COUNT"
  count=$((count + 1))
  printf '%s\n' "$count" > "$CHMOD_COUNT"
  /bin/chmod "$@"
  if [[ "$PERMISSION_FAILURE" == "chmod" && "$count" == 3 ]]; then exit 42; fi
  exit 0
fi
/bin/chmod "$@"
if [[ "$PERMISSION_FAILURE" == "chown" && "$1" == "+t" && "$2" == "$SERVICE_ROOT/.service/archive" ]]; then
  exit 53
fi
exit 0
""",
                encoding="utf-8",
            )
            chmod.chmod(chmod.stat().st_mode | stat.S_IXUSR)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": os.pathsep.join(
                        (str(fake_env), str(fakebin), environment["PATH"])
                    ),
                    "VIRTUAL_ENV": str(root / "venv"),
                    "PYTHON_TRACE": str(python_trace),
                    "DOCKER_TRACE": str(docker_trace),
                    "CHOWN_TRACE": str(chown_trace),
                    "CHMOD_TRACE": str(chmod_trace),
                    "CHMOD_COUNT": str(chmod_count),
                    "SERVICE_ROOT": str(service.resolve()),
                    "PERMISSION_FAILURE": failure,
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
            resulting_modes = {
                relative: stat.S_IMODE((service / relative).stat().st_mode)
                for relative in writable
            }
            chown_calls = chown_trace.read_text(encoding="utf-8").splitlines()
            chmod_calls = chmod_trace.read_text(encoding="utf-8").splitlines()
        return result, original_modes, resulting_modes, chown_calls, chmod_calls

    def test_makefile_exposes_exactly_seven_thin_public_targets(self) -> None:
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
            },
        )
        recipes = [line.strip() for line in source.splitlines() if line.startswith("\t")]
        self.assertEqual(len(recipes), 7)
        self.assertTrue(all("$(SCRIPTS)/" in recipe for recipe in recipes))
        self.assertIn(
            '$(SCRIPTS)/fix-shakemap-permissions.sh --runtime-root "$(RUNTIME_ROOT)"',
            recipes,
        )

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

    def test_helpers_have_no_name_image_or_arbitrary_environment_options(self) -> None:
        for name in ("start-shakemap-docker.sh", "finalize-shakemap.sh"):
            source = (PROJECT / "scripts" / name).read_text(encoding="utf-8")
            for forbidden in ("--name)", "--image)", "--env)"):
                self.assertNotIn(forbidden, source)
        stop = (PROJECT / "scripts/stop-shakemap-docker.sh").read_text(encoding="utf-8")
        self.assertIn("docker stop --time 65", stop)
        self.assertNotIn("docker rm", stop)

    def test_finalization_is_ready_last_and_failure_revokes_readiness(self) -> None:
        source = (PROJECT / "scripts/finalize-shakemap.sh").read_text(encoding="utf-8")
        self.assertIn("--network none", (PROJECT / "scripts/container-configuration.sh").read_text(encoding="utf-8"))
        self.assertIn("python -m shakemap_service.finalization fail", source)
        ready = source.index("python -m shakemap_service.finalization ready")
        parity = source.index('CURRENT_STEP="pre-ready public parity checks"')
        self.assertGreater(ready, parity)
        self.assertIn("docker stop --time 65", source)
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
        failure_handler = source[
            source.index("fail_closed() {") : source.index("trap fail_closed EXIT")
        ]
        self.assertLess(
            failure_handler.index("shakemap_service.finalization fail"),
            failure_handler.index('rm -rf -- "${SEED_STAGING}"'),
        )
        self.assertIn('rm -rf -- "${SEED_STAGING}" >/dev/null 2>&1 || true', failure_handler)
        deployment_verifier = (
            PROJECT / "scripts/verify-shakemap-deployment.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("verify-shakemap-image.sh --deployment", deployment_verifier)
        self.assertIn("verification/request/event.xml", deployment_verifier)
        self.assertIn("verification/request/event_dat.xml", deployment_verifier)
        self.assertNotIn("tests/fixtures/shakemap_scenario", deployment_verifier)

        permission_slice = source[
            source.index('CURRENT_STEP="writable path ownership and access"') :
            source.index('CURRENT_STEP="isolated canonical container creation"')
        ]
        for writable in (
            "products",
            "logs",
            "data/inputs",
            ".service/events",
            ".service/archive",
            ".service/queue",
        ):
            self.assertIn(writable, permission_slice)
        for writable_root in ("products", "logs", ".service", "data/inputs"):
            self.assertIn(writable_root, permission_slice)
        self.assertIn('chmod u+rwx "${path}"', permission_slice)
        self.assertNotIn("chmod -R", permission_slice)
        self.assertIn("restore_writable_special_modes", permission_slice)
        self.assertIn('operation="chmod ${mode}+s"', permission_slice)
        self.assertIn('operation="chmod +t"', permission_slice)
        for scientific in ("data/global", "data/regional", "data/test"):
            self.assertNotIn(scientific, permission_slice)

    def test_finalization_restores_modes_after_partial_chown_failure(self) -> None:
        result, original, resulting, chown_calls, chmod_calls = (
            self._run_finalization_permission_failure("chown")
        )

        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(resulting, original)
        self.assertEqual(len(chown_calls), 3)
        self.assertTrue(chown_calls[-1].endswith("/.service"), chown_calls)
        self.assertFalse(any(call.startswith("u+rwx ") for call in chmod_calls))
        restored = {
            call.removeprefix("+t ")
            for call in chmod_calls
            if call.startswith("+t ")
        }
        self.assertEqual(
            restored,
            {str(Path(result.args[3]).resolve() / "shakemap" / path) for path in original},
        )
        self.assertIn("chown -R 1000:1000 failed", result.stderr)
        self.assertIn("/.service with exit code 37", result.stderr)
        self.assertIn("mode ", result.stderr)
        self.assertIn("UID:GID ", result.stderr)
        self.assertIn("permitted to assign UID:GID 1000:1000", result.stderr)
        self.assertIn("chmod +t failed", result.stderr)
        self.assertIn("/.service/archive with exit code 53", result.stderr)
        self.assertIn("restore this mode as the path owner", result.stderr)

    def test_finalization_restores_modes_after_mid_loop_chmod_failure(self) -> None:
        result, original, resulting, chown_calls, chmod_calls = (
            self._run_finalization_permission_failure("chmod")
        )

        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(len(chown_calls), 4)
        owner_mode_calls = [call for call in chmod_calls if call.startswith("u+rwx ")]
        self.assertEqual(len(owner_mode_calls), 3)
        restored = {
            call.removeprefix("+t ")
            for call in chmod_calls
            if call.startswith("+t ")
        }
        self.assertEqual(
            restored,
            {str(Path(result.args[3]).resolve() / "shakemap" / path) for path in original},
        )
        for path, mode in original.items():
            self.assertEqual(resulting[path] & 0o7077, mode & 0o7077)
        self.assertIn("chmod u+rwx failed", result.stderr)
        self.assertIn("/data/inputs with exit code 42", result.stderr)
        self.assertIn("mode ", result.stderr)
        self.assertIn("UID:GID ", result.stderr)
        self.assertIn("path owner or with sufficient host permission", result.stderr)

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
[[ "$*" == *'prepare-runtime'* ]] && exit 9
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
            docker = fakebin / "docker"
            docker.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_TRACE"
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
  case "$*" in
    *'{{.State.Running}}'*) echo true ;;
    *'range .Mounts'*) echo "$RUNTIME|/home/sysop/runtime" ;;
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
        self.assertEqual(result.returncode, 9)
        self.assertTrue(any("finalization begin" in call for call in python_calls))
        self.assertTrue(any("prepare-runtime" in call for call in python_calls))
        self.assertTrue(any("finalization fail" in call for call in docker_calls))
        self.assertTrue(any(call.startswith("stop --time 65 shakemap-docker") for call in docker_calls))
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
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$PYTHON_TRACE\"\nexit 0\n",
                encoding="utf-8",
            )
            python.chmod(python.stat().st_mode | stat.S_IXUSR)
            cli = fake_env / "shake-in-docker"
            cli.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
            fakebin = root / "bin"
            fakebin.mkdir()
            docker_trace = root / "docker-trace"
            container_state = root / "container-state"
            docker = fakebin / "docker"
            docker.write_text(
                """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_TRACE"
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
  [[ "$*" == *'State.Running'* ]] && echo false
  exit 0
fi
if [[ "$1" == "create" ]]; then touch "$CONTAINER_STATE"; echo fake-container; exit 0; fi
if [[ "$1" == "cp" ]]; then exit 0; fi
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
                    "PYTHON_TRACE": str(python_trace),
                    "DOCKER_TRACE": str(docker_trace),
                    "CONTAINER_STATE": str(container_state),
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
            {directory: mode | 0o700 for directory, mode in original_modes.items()},
        )
        create = next(index for index, call in enumerate(docker_calls) if call.startswith("create "))
        copy = next(index for index, call in enumerate(docker_calls) if call.startswith("cp "))
        start = next(index for index, call in enumerate(docker_calls) if call.startswith("start "))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
