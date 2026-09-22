"""Execute Docker heredoc gates with stdin attachment modeled explicitly."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class ContainerStdinGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="container stdin ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.environment = dict(
            os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
            GATE_FIXTURE=str(self.root),
        )
        self.executable("sleep", "#!/usr/bin/env bash\nexit 0\n")
        self.executable("docker", f"#!{sys.executable}\n" + r'''
import io
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

root = Path(os.environ['GATE_FIXTURE'])
arguments = sys.argv[1:]
with (root / 'docker-calls').open('a') as trace:
    trace.write(json.dumps(arguments) + '\n')
if arguments[0] != 'exec':
    raise SystemExit(0)
# Docker does not forward stdin without -i. Python reading an unattached stdin
# receives EOF and exits successfully without executing any of the heredoc.
program = sys.stdin.read() if '-i' in arguments else ''
sys.argv = arguments[arguments.index('python') + 1:]

def response(url, **kwargs):
    with (root / 'http-calls').open('a') as trace:
        trace.write(url + '\n')
    counter = root / 'response-index'
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    responses = json.loads((root / 'responses.json').read_text())
    value = responses[min(index, len(responses) - 1)]
    if value == 'http-error':
        raise urllib.error.URLError('injected connection failure')
    return io.StringIO(json.dumps(value))

clock = 0
def monotonic():
    global clock
    value = clock
    clock += int(os.environ.get('CLOCK_STEP', '1'))
    return value

urllib.request.urlopen = response
time.sleep = lambda _: None
time.monotonic = monotonic
exec(compile(program, '<docker-stdin>', 'exec'), {'__name__': '__main__'})
''')

    def executable(self, name: str, source: str) -> None:
        target = self.bin / name
        target.write_text(source)
        target.chmod(0o755)

    def run_script(self, script: str, responses: list) -> subprocess.CompletedProcess[str]:
        (self.root / "responses.json").write_text(json.dumps(responses))
        return subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            cwd=PROJECT, env=self.environment, text=True, capture_output=True,
        )

    def docker_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in (self.root / "docker-calls").read_text().splitlines()]

    def http_calls(self) -> list[str]:
        target = self.root / "http-calls"
        return target.read_text().splitlines() if target.exists() else []

    def health_script(self, expected: str) -> str:
        helper = shlex.quote(str(PROJECT / "scripts/container-configuration.sh"))
        return f"source {helper}\nwait_for_container_health {expected}\nprintf 'health gate passed\\n'"

    def polling_script(self) -> str:
        source = (PROJECT / "scripts/finalize-shakemap.sh").read_text()
        start = source.index("docker exec ", source.index('sequence="$(python -c'))
        end = source.index('\nBASE_URL=', start)
        return "\n".join([
            'CANONICAL_CONTAINER="shakemap-docker"', 'EVENT_ID="fixture event"', 'sequence=7',
            'container_run_command() { CONTAINER_COMMAND=(docker run); }',
            'wait_for_container_health() { :; }',
            source[start:end],
        ])

    @staticmethod
    def calculation(state: str, sequence: int = 7) -> dict:
        return {
            "internal_sequence": sequence, "status": state,
            "job_completed": state in ("SUCCESS", "FAILED"),
            "products_ready": state == "SUCCESS",
        }

    def test_health_retries_http_failure_and_wrong_readiness_until_true(self) -> None:
        result = self.run_script(self.health_script("true"), ["http-error", {"ready": False}, {"ready": True}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.http_calls()), 3)
        self.assertEqual(len(self.docker_calls()), 3)
        self.assertIn("health gate passed", result.stdout)

    def test_health_waits_for_false_when_requested(self) -> None:
        result = self.run_script(self.health_script("false"), [{"ready": True}, {"ready": False}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.http_calls()), 2)

    def test_health_never_succeeds_when_expected_state_is_absent(self) -> None:
        result = self.run_script(self.health_script("true"), [{"ready": False}])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.http_calls()), 60)
        self.assertNotIn("health gate passed", result.stdout)
        self.assertIn("did not report ready=true", result.stderr)

    def test_poll_waits_for_matching_sequence_success_before_publication(self) -> None:
        responses = [
            {"jobs": [self.calculation("SUCCESS", 6), self.calculation("QUEUED")]},
            {"jobs": [self.calculation("RUNNING")]},
            {"jobs": [self.calculation("SUCCESS")]},
            {"current": self.calculation("SUCCESS")},
        ]
        result = self.run_script(self.polling_script(), responses)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.http_calls()), 4)
        self.assertTrue(self.http_calls()[-1].endswith("/products"))
        self.assertEqual([call[0] for call in self.docker_calls()], ["exec", "stop", "rm", "run"])

    def test_failed_calculation_prevents_publication(self) -> None:
        result = self.run_script(self.polling_script(), [{"jobs": [self.calculation("FAILED")]}])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not satisfy the SUCCESS gate", result.stderr)
        self.assertEqual([call[0] for call in self.docker_calls()], ["exec"])
        self.assertEqual(len(self.http_calls()), 1)

    def test_polling_http_failure_prevents_publication(self) -> None:
        result = self.run_script(self.polling_script(), ["http-error"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("injected connection failure", result.stderr)
        self.assertEqual([call[0] for call in self.docker_calls()], ["exec"])

    def test_product_sequence_mismatch_prevents_publication(self) -> None:
        result = self.run_script(self.polling_script(), [
            {"jobs": [self.calculation("SUCCESS")]},
            {"current": self.calculation("SUCCESS", 6)},
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the successful sequence", result.stderr)
        self.assertEqual([call[0] for call in self.docker_calls()], ["exec"])

    def test_polling_deadline_prevents_publication(self) -> None:
        self.environment["CLOCK_STEP"] = "1801"
        result = self.run_script(self.polling_script(), [{"jobs": [self.calculation("RUNNING")]}])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not reach a terminal state", result.stderr)
        self.assertEqual([call[0] for call in self.docker_calls()], ["exec"])


if __name__ == "__main__":
    unittest.main()
