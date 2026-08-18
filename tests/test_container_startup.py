#!/usr/bin/env python3
"""Host tests for finalized-only canonical container startup."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
START = PROJECT / "scripts/start-shakemap-docker.sh"
ENTRYPOINT = PROJECT / "entrypoint.sh"
CONFIGURATION = PROJECT / "scripts/container-configuration.sh"
STOP = PROJECT / "scripts/stop-shakemap-docker.sh"


class ContainerStartupTests(unittest.TestCase):
    def _run_matching_start(
        self,
        state: str,
        *,
        mismatch: bool = False,
        configured_max: str = "3",
        readiness_match: bool = True,
        configured_shared: str | None = None,
        configuration_change: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
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
        python = fake_env / "python"
        python.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$*\" == *'finalization check-ready'* && \"$READINESS_MATCH\" != 1 ]]; then exit 1; fi\n"
            "if [[ \"$1\" == - ]]; then exec \"$REAL_PYTHON\" \"$@\"; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        cli = fake_env / "shake-in-docker"
        cli.write_text("#!/usr/bin/env bash\necho '{\"ready\":true}'\n", encoding="utf-8")
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        fakebin = root / "bin"
        fakebin.mkdir()
        trace = root / "trace"
        image_id = "sha256:" + "a" * 64
        image_environment = ["PATH=/usr/bin", "LANG=C.UTF-8"]
        container_environment = [
            *image_environment,
            f"SHAKEMAP_IMAGE_ID={image_id}",
            f"SHAKEMAP_SHARED_RUNTIME_ROOT={configured_shared or runtime.resolve()}",
            f"SHAKEMAP_MAX_CONCURRENT={configured_max}",
        ]
        mounts = [
            {
                "Type": "bind",
                "Source": str(runtime.resolve()),
                "Destination": "/home/sysop/runtime",
                "RW": True,
            },
            *[
                {
                    "Type": "bind",
                    "Source": str(service.resolve() / f"data/{name}"),
                    "Destination": f"/home/sysop/runtime/shakemap/data/{name}",
                    "RW": False,
                }
                for name in ("global", "regional", "test")
            ],
        ]
        ports = {"9010/tcp": [{"HostIp": "", "HostPort": "19010"}]}
        network_mode = "default"
        if configuration_change == "extra_mount":
            mounts.append(
                {
                    "Type": "bind",
                    "Source": str(root),
                    "Destination": "/unexpected",
                    "RW": False,
                }
            )
        elif configuration_change == "extra_env":
            container_environment.append("UNSUPPORTED=value")
        elif configuration_change == "extra_port":
            ports["9999/tcp"] = [{"HostIp": "", "HostPort": "19999"}]
        elif configuration_change == "host_ip":
            ports["9010/tcp"][0]["HostIp"] = "127.0.0.1"
        elif configuration_change == "network_mode":
            network_mode = "bridge"
        container_details = {
            "Name": "/shakemap-docker",
            "Image": "sha256:" + ("b" if mismatch else "a") * 64,
            "Config": {
                "Image": "shakemap-docker:latest",
                "Env": container_environment,
            },
            "Mounts": mounts,
            "HostConfig": {
                "PortBindings": ports,
                "NetworkMode": network_mode,
            },
        }
        docker = fakebin / "docker"
        docker.write_text(
            """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TRACE"
image_id='sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
if [[ "$1 $2" == "image inspect" ]]; then
  case "$*" in
    *'{{json .Config.Env}}'*) echo "$IMAGE_ENVIRONMENT_JSON" ;;
    *'{{.Id}}'*) echo "$image_id" ;;
    *'org.usgs.shakemap.release'*) echo 'v4.4.9' ;;
    *'org.usgs.shakemap.version'*) echo '4.4.9' ;;
    *'org.usgs.shakemap.commit'*) echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
    *) echo '' ;;
  esac
  exit 0
fi
if [[ "$1 $2" == "container inspect" ]]; then
  [[ "$FAKE_STATE" == "absent" ]] && exit 1
  if [[ "$4" == '{{json .}}' ]]; then echo "$CONTAINER_JSON"
  elif [[ "$4" =~ State.Running ]]; then [[ "$FAKE_STATE" == "running" ]] && echo true || echo false
  fi
  exit 0
fi
if [[ "$1 $2" == "container ls" ]]; then
  [[ "$FAKE_STATE" != "absent" ]] && echo shakemap-docker
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
                "TRACE": str(trace),
                "FAKE_STATE": state,
                "MISMATCH": "1" if mismatch else "0",
                "CONFIGURED_MAX": configured_max,
                "CONFIGURED_SHARED": configured_shared or str(runtime.resolve()),
                "CONTAINER_JSON": json.dumps(container_details),
                "IMAGE_ENVIRONMENT_JSON": json.dumps(image_environment),
                "READINESS_MATCH": "1" if readiness_match else "0",
                "REAL_PYTHON": sys.executable,
                "RUNTIME": str(runtime.resolve()),
                "SERVICE": str(service.resolve()),
            }
        )
        result = subprocess.run(
            [
                "bash",
                str(START),
                "--runtime-root",
                str(runtime),
                "--port",
                "19010",
                "--max-concurrent",
                "3",
            ],
            cwd=PROJECT,
            env=environment,
            capture_output=True,
            text=True,
        )
        return result, trace.read_text(encoding="utf-8").splitlines()

    def _run_stop(self, state: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fakebin = root / "bin"
        fakebin.mkdir()
        trace = root / "trace"
        docker = fakebin / "docker"
        docker.write_text(
            """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TRACE"
if [[ "$1 $2" == "container ls" ]]; then
  case "$FAKE_STATE" in
    absent) exit 0 ;;
    missing-command) exit 127 ;;
    daemon-error) exit 20 ;;
    permission-error) exit 21 ;;
    malformed-list) echo 'unexpected'; exit 0 ;;
    *) echo 'shakemap-docker'; exit 0 ;;
  esac
fi
if [[ "$1 $2" == "container inspect" ]]; then
  [[ "$FAKE_STATE" == "inspect-error" ]] && exit 22
  [[ "$FAKE_STATE" == "malformed-state" ]] && { echo unknown; exit 0; }
  [[ "$FAKE_STATE" == "running" ]] && echo true || echo false
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
                "PATH": os.pathsep.join((str(fakebin), environment["PATH"])),
                "TRACE": str(trace),
                "FAKE_STATE": state,
            }
        )
        result = subprocess.run(
            ["bash", str(STOP)],
            cwd=PROJECT,
            env=environment,
            capture_output=True,
            text=True,
        )
        return result, trace.read_text(encoding="utf-8").splitlines()

    def test_fixed_container_and_image_have_no_operator_override(self) -> None:
        configuration = CONFIGURATION.read_text(encoding="utf-8")
        start = START.read_text(encoding="utf-8")
        self.assertIn('CANONICAL_IMAGE="shakemap-docker:latest"', configuration)
        self.assertIn('CANONICAL_CONTAINER="shakemap-docker"', configuration)
        for option in ("--name)", "--image)", "--env)", "--data)"):
            self.assertNotIn(option, start)
        self.assertIn("check-ready", start)

    def test_entrypoint_creates_only_service_writable_paths(self) -> None:
        source = ENTRYPOINT.read_text(encoding="utf-8")
        for writable in (
            "products",
            "logs",
            "data/inputs",
            ".service/events",
            ".service/archive",
            ".service/queue",
        ):
            self.assertIn(writable, source)
        for external in ("data/global/vs30", "data/global/topo", ".service/preparation"):
            self.assertNotIn(external, source)
        self.assertNotIn("SHAKEMAP_REQUIRE_MOUNT", source)
        self.assertNotIn("chmod ", source)
        self.assertIn('touch "${DIRPATH}/.writetest_$$"', source)
        self.assertIn('rm -f "${DIRPATH}/.writetest_$$"', source)

    def test_external_data_mounts_are_separate_read_only_overlays(self) -> None:
        source = CONFIGURATION.read_text(encoding="utf-8")
        self.assertIn("data/inputs", source)
        for name in ("global", "regional", "test"):
            self.assertIn(
                f'data/{name}:/home/sysop/runtime/shakemap/data/{name}:ro',
                source,
            )
        self.assertNotIn("/home/sysop/runtime/shakemap/data:ro", source)

    def test_help_and_makefile_use_supported_settings(self) -> None:
        help_text = subprocess.run(
            ["bash", str(START), "--help"],
            cwd=PROJECT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for option in ("--runtime-root", "--port", "--max-concurrent"):
            self.assertIn(option, help_text)
        makefile = (PROJECT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("RUNTIME_ROOT ?= ./runtime", makefile)
        self.assertIn("PORT ?= 9010", makefile)
        self.assertIn("MAX_CONCURRENT ?= 10", makefile)

    def test_missing_runtime_refuses_before_any_docker_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fakebin = root / "bin"
            fakebin.mkdir()
            trace = root / "trace"
            docker = fakebin / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$TRACE\"\n",
                encoding="utf-8",
            )
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join((str(fakebin), environment["PATH"]))
            environment["TRACE"] = str(trace)
            result = subprocess.run(
                [
                    "bash",
                    str(START),
                    "--runtime-root",
                    str(root / "missing"),
                ],
                cwd=PROJECT,
                env=environment,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime root does not exist", result.stderr)
        self.assertFalse(trace.exists())

    def test_removed_customization_options_are_rejected(self) -> None:
        for option, value in (
            ("--name", "other"),
            ("--image", "other:latest"),
            ("--env", "KEY=value"),
            ("--data", "/tmp/data"),
        ):
            with self.subTest(option=option):
                result = subprocess.run(
                    ["bash", str(START), option, value],
                    cwd=PROJECT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"unknown option: {option}", result.stderr)

    def test_absent_matching_finalized_deployment_is_created(self) -> None:
        result, trace = self._run_matching_start("absent")
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + "\n".join(trace))
        run = next(line for line in trace if line.startswith("run "))
        self.assertIn("--name shakemap-docker", run)
        self.assertIn("-p 19010:9010", run)
        self.assertTrue(run.endswith("shakemap-docker:latest"), run)

    def test_stopped_matching_deployment_is_started_without_recreation(self) -> None:
        result, trace = self._run_matching_start("stopped")
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + "\n".join(trace))
        self.assertIn("start shakemap-docker", trace)
        self.assertFalse(any(line.startswith("run ") for line in trace))

    def test_running_matching_deployment_is_left_running(self) -> None:
        result, trace = self._run_matching_start("running")
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + "\n".join(trace))
        self.assertFalse(any(line.startswith("run ") for line in trace))
        self.assertFalse(any(line.startswith("start ") for line in trace))

    def test_concurrency_requires_an_exact_environment_entry(self) -> None:
        result, trace = self._run_matching_start("running", configured_max="30")
        self.assertNotEqual(result.returncode, 0, "\n".join(trace))
        self.assertIn("environment", result.stderr)

    def test_readiness_mismatch_refuses_before_container_mutation(self) -> None:
        result, trace = self._run_matching_start("running", readiness_match=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readiness does not match", result.stderr)
        self.assertFalse(
            any(
                line.startswith(("run ", "start ", "stop ", "rm "))
                for line in trace
            ),
            trace,
        )

    def test_shared_runtime_mismatch_refuses_existing_container(self) -> None:
        result, trace = self._run_matching_start(
            "running",
            configured_shared="/wrong/operator/runtime",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment", result.stderr)
        self.assertFalse(
            any(line.startswith(("run ", "start ", "stop ", "rm ")) for line in trace),
            trace,
        )

    def test_mismatched_deployment_refuses_without_mutation(self) -> None:
        result, trace = self._run_matching_start("running", mismatch=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image-id", result.stderr)
        self.assertFalse(any(line.startswith("run ") for line in trace))
        self.assertFalse(any(line.startswith("start ") for line in trace))

    def test_extra_container_configuration_is_rejected_exactly(self) -> None:
        for change, expected in (
            ("extra_mount", "mounts"),
            ("extra_env", "environment"),
            ("extra_port", "ports"),
            ("host_ip", "ports"),
            ("network_mode", "network-mode"),
        ):
            with self.subTest(change=change):
                result, trace = self._run_matching_start(
                    "running",
                    configuration_change=change,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertFalse(
                    any(line.startswith(("run ", "start ", "stop ", "rm ")) for line in trace),
                    trace,
                )

    def test_stop_is_idempotent_and_stops_only_a_running_canonical_container(self) -> None:
        for state in ("absent", "stopped", "running"):
            with self.subTest(state=state):
                result, trace = self._run_stop(state)
                self.assertEqual(result.returncode, 0, result.stderr)
                stops = [line for line in trace if line.startswith("stop ")]
                expected = ["stop --time 65 shakemap-docker"] if state == "running" else []
                self.assertEqual(stops, expected)
                self.assertFalse(any(line.startswith("rm ") for line in trace))

    def test_stop_propagates_docker_and_malformed_state_failures(self) -> None:
        for state in (
            "missing-command",
            "daemon-error",
            "permission-error",
            "malformed-list",
            "inspect-error",
            "malformed-state",
        ):
            with self.subTest(state=state):
                result, trace = self._run_stop(state)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(line.startswith("stop ") for line in trace))
                self.assertFalse(any(line.startswith("rm ") for line in trace))


if __name__ == "__main__":
    unittest.main(verbosity=2)
