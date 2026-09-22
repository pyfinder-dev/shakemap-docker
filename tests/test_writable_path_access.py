"""Host-only behavior checks for writable probes and bounded permission repair."""
from __future__ import annotations

import json
import errno
import io
import contextlib
import os
from pathlib import Path
import stat
import subprocess
import shlex
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / "scripts/repair-shakemap-writable-paths.sh"


class WritablePathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="writable paths ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runtime = self.root / "runtime with 'quote"
        self.service = self.runtime / "shakemap"
        for relative in ("products", "logs", ".service/events", ".service/archive",
                         ".service/queue", "data/inputs", "data/global", "data/regional", "data/test"):
            (self.service / relative).mkdir(parents=True)
        (self.service / ".service/workflow.lock").write_text("existing lock")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "repair-trace"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        FIXTURE_ROOT=str(self.root), RUNTIME_FIXTURE=str(self.runtime),
                        SERVICE_FIXTURE=str(self.service), REAL_PYTHON=sys.executable)
        self.executable("chown", """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FIXTURE_ROOT/repair-trace"
if [[ "${REPAIR_FAIL:-}" == 1 ]]; then
  echo "chown: $3: Operation not permitted" >&2
  /bin/chmod -s "$3"
  exit 37
fi
exit 0
""")

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)
        return path

    def helper(self):
        return subprocess.run(["bash", str(HELPER), "--runtime-root", str(self.runtime)],
                              env=self.env, capture_output=True, text=True)

    def test_repair_preserves_content_and_modes_excludes_operator_trees(self):
        nested = self.service / "products/nested"
        nested.mkdir()
        nested.chmod(0o2751)
        nested_mode = stat.S_IMODE(nested.stat().st_mode)
        state = self.service / ".service/workflow.lock"
        state.chmod(0o4640)
        before = stat.S_IMODE(state.stat().st_mode)
        scientific = self.service / "data/global/asset"
        scientific.write_text("operator")
        scientific.chmod(0o400)
        result = self.helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state.read_text(), "existing lock")
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), before | 0o600)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), nested_mode | 0o700)
        self.assertEqual(stat.S_IMODE(scientific.stat().st_mode), 0o400)
        calls = self.trace.read_text()
        for excluded in ("data/global", "data/regional", "data/test"):
            self.assertNotIn(excluded, calls)
        self.assertNotIn(" -R ", calls)

    def test_entire_preflight_precedes_mutation(self):
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                path = self.service / "data/inputs/unsafe"
                if kind == "symlink":
                    path.symlink_to(self.service / "data/global")
                elif kind == "hardlink":
                    os.link(self.service / ".service/workflow.lock", path)
                else:
                    os.mkfifo(path)
                result = self.helper()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.trace.exists())
                self.assertIn(str(self.service), result.stderr)
                self.assertIn("sudo does not resolve", result.stderr)
                path.unlink()

    def test_partial_chown_failure_restores_special_modes(self):
        root = self.service / "products"
        root.chmod(0o3751)
        original = stat.S_IMODE(root.stat().st_mode)
        self.env["REPAIR_FAIL"] = "1"
        result = self.helper()
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), original)
        self.assertIn("chown -h 1000:1000", result.stderr)
        self.assertIn("mode/owner", result.stderr)
        self.assertIn("sudo ", result.stderr)
        self.assertIn("rerun finalization normally", result.stderr)
        self.assertIn("Operation not permitted", result.stderr)
        self.assertIn("Only if insufficient authorization is confirmed", result.stderr)
        recovery = next(line for line in result.stderr.splitlines() if line.startswith("sudo "))
        self.assertEqual(shlex.split(recovery), ["sudo", str(HELPER), "--runtime-root", str(self.runtime)])

    def assert_unclassified_recovery(self, result, operation, path):
        self.assertIn(operation, result.stderr)
        self.assertIn(str(path), result.stderr)
        self.assertIn("cause of this utility failure is not classified", result.stderr)
        self.assertIn("storage, mount, tool, or layout failures require their own remedy", result.stderr)
        lines = result.stderr.splitlines()
        command_index = next(index for index, line in enumerate(lines) if line.startswith("sudo "))
        self.assertEqual(
            lines[command_index - 1],
            "Only if insufficient authorization is confirmed, with the service stopped, run manually:",
        )
        self.assertEqual(
            shlex.split(lines[command_index]),
            ["sudo", str(HELPER), "--runtime-root", str(self.runtime)],
        )
        self.assertNotIn("With the service stopped, run manually: sudo", result.stderr)

    def test_preflight_io_failure_keeps_error_and_conditions_escalation(self):
        self.executable("find", """#!/usr/bin/env bash
echo "find: $1: Input/output error" >&2
exit 1
""")
        result = self.helper()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Input/output error", result.stderr)
        self.assert_unclassified_recovery(result, "preflight traversal", self.service / "products")
        self.assertFalse(self.trace.exists())

    def test_chmod_read_only_failure_preserves_error_status_and_stops(self):
        path = self.service / "products"
        before = stat.S_IMODE(path.stat().st_mode)
        self.executable("chmod", """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FIXTURE_ROOT/chmod-trace"
if [[ ! -e "$FIXTURE_ROOT/chmod-failed" ]]; then
  touch "$FIXTURE_ROOT/chmod-failed"
  echo "chmod: $2: Read-only file system" >&2
  exit 42
fi
/bin/chmod "$@"
""")
        result = self.helper()
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("Read-only file system", result.stderr)
        self.assert_unclassified_recovery(result, "chmod", path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), before)
        # The only follow-up mutation is the existing attempted mode restoration
        # for this same path; other writable roots must not be processed.
        calls = (self.root / "chmod-trace").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(line.endswith(str(path)) for line in calls))
        self.assertEqual(len(self.trace.read_text().splitlines()), 1)

    def test_chmod_failure_restores_previous_mode(self):
        path = self.service / "products"
        path.chmod(0o1750)
        before = stat.S_IMODE(path.stat().st_mode)
        self.executable("chmod", """#!/usr/bin/env bash
if [[ ! -e "$FIXTURE_ROOT/chmod-failed" ]]; then
  touch "$FIXTURE_ROOT/chmod-failed"
  /bin/chmod 0700 "$2"
  exit 42
fi
/bin/chmod "$@"
""")
        result = self.helper()
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), before)
        self.assertIn("chmod", result.stderr)

    def test_symlink_root_rejected_before_mutation(self):
        products = self.service / "products"
        products.rmdir()
        products.symlink_to(self.service / "data/global")
        result = self.helper()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.trace.exists())

    def finalizer(self, scenario="pass"):
        venv = self.root / "venv/bin"
        venv.mkdir(parents=True)
        python = venv / "python"
        python.write_text("""#!/usr/bin/env bash
if [[ "$1" == - && "${2:-}" == probe ]]; then exec "$REAL_PYTHON" "$@"; fi
exit 0
""")
        python.chmod(0o755)
        cli = venv / "shake-in-docker"
        cli.write_text("#!/usr/bin/env bash\nexit 0\n")
        cli.chmod(0o755)
        self.env.update(VIRTUAL_ENV=str(venv.parent), PATH=f"{venv}:{self.env['PATH']}", SCENARIO=scenario)
        self.executable("docker", f"#!{sys.executable}\n" + r'''
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
root = Path(os.environ['FIXTURE_ROOT'])
state = root / 'container.json'
scenario = os.environ['SCENARIO']
with (root / 'docker-trace').open('a') as log: log.write(json.dumps(a) + '\n')
image = 'sha256:' + 'a' * 64
if a[:2] == ['image', 'inspect']:
    fmt = a[3]
    print('[]' if 'Config.Env' in fmt else image if '.Id' in fmt else '4.4.9' if 'version' in fmt else 'v4.4.9' if 'release' in fmt else '')
elif a[:2] == ['container', 'ls']:
    if state.exists(): print('shakemap-docker')
elif a[:2] == ['container', 'inspect']:
    if not state.exists(): sys.exit(1)
    data = json.loads(state.read_text())
    fmt = a[3] if '--format' in a else ''
    if 'json .' in fmt: print(json.dumps(data))
    elif 'State.Status' in fmt: print('exited:' + str(data['code']) + ':')
    elif 'State.Running' in fmt: print('false')
elif a[0] == 'rm': state.unlink(missing_ok=True)
elif a[0] == 'create':
    probe = '--entrypoint' in a
    mounts, env = [], []
    for i, value in enumerate(a):
        if value == '-v':
            parts = a[i+1].split(':')
            mounts.append(dict(Type='bind', Source=parts[0], Destination=parts[1], RW=len(parts) == 2))
        if value == '-e': env.append(a[i+1])
    data = dict(Name='/shakemap-docker', Image=image,
                Config=dict(Image=image if probe else 'shakemap-docker:latest', Env=env,
                            User='0:0' if scenario == 'wrong-user' else '1000:1000', Entrypoint=['python']),
                HostConfig=dict(NetworkMode='none', PortBindings={}), Mounts=mounts, code=0, probe=probe)
    if scenario == 'wrong-mount': data['Mounts'][0]['RW'] = False
    if probe: (root / 'probe.py').write_text(a[a.index('-c')+1])
    state.write_text(json.dumps(data))
elif a[0] == 'start':
    data = json.loads(state.read_text())
    if not data['probe']: sys.exit(17)
    countfile = root / 'probe-count'
    count = int(countfile.read_text()) + 1 if countfile.exists() else 1
    countfile.write_text(str(count))
    code = 73 if scenario in ('denied', 'repair-fails') or (scenario == 'repair-pass' and count == 1) else 125 if scenario == 'docker-error' else 0
    if scenario == 'filesystem-error': code = 1
    data['code'] = code
    state.write_text(json.dumps(data))
    sys.exit(code)
''')
        return subprocess.run(["bash", str(PROJECT / "scripts/finalize-shakemap.sh"),
                               "--runtime-root", str(self.runtime)],
                              env=self.env, capture_output=True, text=True)

    def test_writable_probe_skips_repair_and_reaches_service_start(self):
        result = self.finalizer()
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertFalse(self.trace.exists())
        calls = [json.loads(line) for line in (self.root / "docker-trace").read_text().splitlines()]
        create = next(call for call in calls if call[0] == "create")
        self.assertEqual(create[create.index("--user") + 1], "1000:1000")
        self.assertEqual(create[create.index("--network") + 1], "none")
        self.assertNotIn("-p", create)
        self.assertEqual(create.count("-v"), 4)
        self.assertEqual(create[create.index("--name") + 1], "shakemap-docker")

    def test_denied_then_repaired_and_reprobed(self):
        result = self.finalizer("repair-pass")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertTrue(self.trace.exists())
        self.assertEqual((self.root / "probe-count").read_text(), "2")

    def test_failed_repair_stops_without_reprobe(self):
        self.env["REPAIR_FAIL"] = "1"
        result = self.finalizer("repair-fails")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual((self.root / "probe-count").read_text(), "1")
        self.assertIn("repair-shakemap-writable-paths.sh", result.stderr)
        self.assertNotIn("For confirmed service-writable permission failures only", result.stderr)

    def test_generic_helper_failure_is_not_reclassified_by_finalizer(self):
        self.executable("find", """#!/usr/bin/env bash
echo "find: $1: Input/output error" >&2
exit 1
""")
        result = self.finalizer("repair-fails")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Input/output error", result.stderr)
        self.assert_unclassified_recovery(result, "preflight traversal", self.service / "products")
        self.assertNotIn("For confirmed service-writable permission failures only", result.stderr)
        self.assertNotIn("Run manually: sudo", result.stderr)
        self.assertFalse(self.trace.exists())
        self.assertEqual((self.root / "probe-count").read_text(), "1")
        retained = json.loads((self.root / "container.json").read_text())
        self.assertTrue(retained["probe"])
        self.assertEqual(retained["code"], 73)
        calls = [json.loads(line) for line in (self.root / "docker-trace").read_text().splitlines()]
        self.assertEqual(sum(call[0] == "create" for call in calls), 1)
        self.assertFalse(any(call[0] == "rm" for call in calls))

    def test_denied_after_repair_retains_stopped_probe(self):
        result = self.finalizer("denied")
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual((self.root / "probe-count").read_text(), "2")
        self.assertTrue(json.loads((self.root / "container.json").read_text())["probe"])
        self.assertIn("Do not run the entire finalizer with sudo", result.stderr)

    def test_infrastructure_failure_does_not_repair(self):
        result = self.finalizer("docker-error")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.trace.exists())
        self.assertIn("probe execution failed", result.stderr)

    def test_non_permission_probe_failure_does_not_run_repair(self):
        result = self.finalizer("filesystem-error")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.trace.exists())
        self.assertNotIn("Run manually: sudo", result.stderr)
        self.assertTrue(json.loads((self.root / "container.json").read_text())["probe"])

    def test_wrong_user_prevents_probe_execution(self):
        result = self.finalizer("wrong-user")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.trace.exists())
        self.assertFalse((self.root / "probe-count").exists())
        self.assertIn("probe-user-or-entrypoint", result.stderr)

    def test_wrong_mount_prevents_probe_execution(self):
        result = self.finalizer("wrong-mount")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.trace.exists())
        self.assertFalse((self.root / "probe-count").exists())
        self.assertIn("mounts", result.stderr)

    def test_real_probe_operations_and_cleanup_on_rename_failure(self):
        self.finalizer()
        code = (self.root / "probe.py").read_text()
        # Execute the exact probe against host fixtures, replacing only its
        # fixed mount path and identity check; Docker mapping remains unproven.
        code = code.replace("Path('/home/sysop/runtime/shakemap')", f"Path({str(self.service)!r})")
        import unittest.mock as mock
        scope = {"__name__": "__main__"}
        with mock.patch("os.getuid", return_value=1000), mock.patch("os.getgid", return_value=1000), mock.patch.object(sys, "argv", ["probe", str(self.service)]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                exec(code, scope)
            self.assertEqual(stopped.exception.code, 0)
            with mock.patch.object(Path, "rename", side_effect=PermissionError(errno.EACCES, "rename denied")):
                with self.assertRaises(SystemExit) as stopped:
                    exec(code, scope)
                self.assertEqual(stopped.exception.code, 73)
        self.assertFalse(list(self.service.rglob(".shakemap-write-probe-*")))
        self.assertEqual((self.service / ".service/workflow.lock").read_text(), "existing lock")

    def test_probe_reports_lock_denial(self):
        self.finalizer()
        code = (self.root / "probe.py").read_text().replace("Path('/home/sysop/runtime/shakemap')", f"Path({str(self.service)!r})")
        import unittest.mock as mock
        real_open = os.open
        def denied_lock(path, flags, *args, **kwargs):
            if Path(path).name == "workflow.lock":
                raise PermissionError(errno.EACCES, "lock denied")
            return real_open(path, flags, *args, **kwargs)
        output = io.StringIO()
        with mock.patch("os.getuid", return_value=1000), mock.patch("os.getgid", return_value=1000), mock.patch.object(sys, "argv", ["probe", str(self.service)]), mock.patch("os.open", side_effect=denied_lock), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as stopped:
                exec(code, {"__name__": "__main__"})
        self.assertEqual(stopped.exception.code, 73)
        self.assertIn(str(self.service / ".service/workflow.lock"), output.getvalue())
        self.assertFalse(list(self.service.rglob(".shakemap-write-probe-*")))

    def test_non_permission_filesystem_errors_do_not_authorize_repair(self):
        self.finalizer()
        code = (self.root / "probe.py").read_text().replace(
            "Path('/home/sysop/runtime/shakemap')", f"Path({str(self.service)!r})"
        )
        import unittest.mock as mock
        with mock.patch("os.getuid", return_value=1000), mock.patch("os.getgid", return_value=1000), mock.patch.object(sys, "argv", ["probe", str(self.service)]):
            for error in (errno.ENOSPC, errno.EIO, errno.EROFS):
                with self.subTest(errno=error), contextlib.redirect_stderr(io.StringIO()) as output:
                    with mock.patch("tempfile.mkstemp", side_effect=OSError(error, os.strerror(error))):
                        with self.assertRaises(SystemExit) as stopped:
                            exec(code, {"__name__": "__main__"})
                    self.assertEqual(stopped.exception.code, 1)
                    self.assertIn(os.strerror(error), output.getvalue())
        self.assertFalse(self.trace.exists())
        self.assertFalse(list(self.service.rglob(".shakemap-write-probe-*")))

    def test_non_permission_error_takes_precedence_over_permission_denial(self):
        self.finalizer()
        code = (self.root / "probe.py").read_text().replace(
            "Path('/home/sysop/runtime/shakemap')", f"Path({str(self.service)!r})"
        )
        import unittest.mock as mock
        def denied_create(*args, **kwargs):
            error = errno.EACCES if Path(kwargs["dir"]).name == "products" else errno.ENOSPC
            raise OSError(error, os.strerror(error))
        with mock.patch("os.getuid", return_value=1000), mock.patch("os.getgid", return_value=1000), mock.patch.object(sys, "argv", ["probe", str(self.service)]), mock.patch("tempfile.mkstemp", side_effect=denied_create), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                exec(code, {"__name__": "__main__"})
        self.assertEqual(stopped.exception.code, 1)
        self.assertFalse(self.trace.exists())


if __name__ == "__main__":
    unittest.main()
