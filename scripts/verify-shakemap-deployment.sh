#!/usr/bin/env bash
# Verify host code, the canonical container, and the running public service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
source "${SCRIPT_DIR}/container-configuration.sh"

RUNTIME_ROOT="${RUNTIME_ROOT:-${PROJECT_ROOT}/runtime}"
PORT="${PORT:-9010}"
MAX_CONCURRENT="${MAX_CONCURRENT:-10}"
FAILURE_ARMED=0
CURRENT_STEP="host-side tests"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime-root) RUNTIME_ROOT="${2:?--runtime-root requires a directory}"; shift 2 ;;
        --port) PORT="${2:?--port requires a value}"; shift 2 ;;
        --max-concurrent) MAX_CONCURRENT="${2:?--max-concurrent requires a value}"; shift 2 ;;
        --help|-h) echo "Usage: $0 [--runtime-root DIR] [--port PORT] [--max-concurrent COUNT]"; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done

require_project_environment
validate_deployment_settings

fail_closed() {
    local result=$?
    trap - EXIT
    if [[ "${FAILURE_ARMED}" == 1 ]]; then
        local reason="deployment verification failed during ${CURRENT_STEP} with exit code ${result}; correct the reported error and rerun make finalize"
        local recorded=0
        if [[ "$(docker container inspect --format '{{.State.Running}}' "${CANONICAL_CONTAINER}" 2>/dev/null || true)" == "true" ]] && \
            docker exec "${CANONICAL_CONTAINER}" python -m shakemap_service.finalization fail \
                --reason "${reason}" >/dev/null 2>&1; then
            recorded=1
        fi
        if [[ "${recorded}" == 0 ]]; then
            RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
                python -m shakemap_service.finalization fail \
                    --reason "${reason}" >/dev/null 2>&1 || true
        fi
        if [[ "$(docker container inspect --format '{{.State.Running}}' "${CANONICAL_CONTAINER}" 2>/dev/null || true)" == "true" ]]; then
            docker stop --time 65 "${CANONICAL_CONTAINER}" >/dev/null 2>&1 || true
        fi
    fi
    exit "${result}"
}

echo "== Host-side tests =="
cd "${PROJECT_ROOT}"
python -m unittest discover -s tests -p 'test_*.py'

echo "== Container-internal checks =="
CURRENT_STEP="canonical container ownership checks"
resolve_runtime_root
load_image_identity
verify_canonical_container_configuration published
running="$(docker container inspect --format '{{.State.Running}}' "${CANONICAL_CONTAINER}")"
if [[ "${running}" != "true" ]]; then
    echo "ERROR: canonical container is not running; verification does not start it." >&2
    exit 1
fi
FAILURE_ARMED=1
trap fail_closed EXIT
CURRENT_STEP="container-internal checks"
docker exec "${CANONICAL_CONTAINER}" /app/scripts/verify-shakemap-image.sh --deployment

echo "== Running-service checks =="
CURRENT_STEP="running-service checks"
BASE_URL="http://127.0.0.1:${PORT}"
python - "${BASE_URL}" "${PROJECT_CLI}" <<'PY'
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

base, cli = sys.argv[1:]
event_id = "shakemap-docker-verification"

def rest(method, path, body=None, content_type=None):
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

def command(*arguments):
    result = subprocess.run(
        [cli, "--url", base, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)

pairs = (
    ("health", "/healthz"),
    ("config", "/config"),
    ("configurations", "/configurations"),
    ("list", "/events"),
    ("queue", "/queue"),
)
for name, path in pairs:
    if command(name) != rest("GET", path):
        raise SystemExit(f"REST/CLI mismatch for {name}")
if command("health").get("ready") is not True:
    raise SystemExit("canonical deployment is not ready")

accepted = command(
    "submit",
    event_id,
    "--configuration",
    "global",
    "--overwrite",
    "true",
    "--file",
    str((__import__("pathlib").Path.cwd() / "tests/fixtures/shakemap_scenario/event.xml")),
    "--file",
    str((__import__("pathlib").Path.cwd() / "tests/fixtures/shakemap_scenario/event_dat.xml")),
)
sequence = accepted["internal_sequence"]
encoded = urllib.parse.quote(event_id, safe="")
deadline = time.monotonic() + 1800
while time.monotonic() < deadline:
    detail = rest("GET", f"/events/{encoded}")
    current = next(
        (job for job in detail["jobs"] if job["internal_sequence"] == sequence),
        None,
    )
    if current and current["status"] in {"SUCCESS", "FAILED"}:
        break
    time.sleep(2)
else:
    raise SystemExit("verification calculation did not finish")
if current["status"] != "SUCCESS" or current["job_completed"] is not True or current["products_ready"] is not True:
    raise SystemExit("verification calculation failed the service success gate")
if command("status", event_id) != detail:
    raise SystemExit("REST/CLI mismatch for event detail")
products = rest("GET", f"/events/{encoded}/products")
if command("products", event_id) != products:
    raise SystemExit("REST/CLI mismatch for products")
if products["current"]["internal_sequence"] != sequence or products["current"]["products_ready"] is not True:
    raise SystemExit("current products do not match the successful sequence")
if command("list") != rest("GET", "/events") or command("queue") != rest("GET", "/queue"):
    raise SystemExit("post-calculation collection or queue parity failed")
print(f"verified sequence {sequence}")
PY

FAILURE_ARMED=0
trap - EXIT
echo "All three verification sections passed."
