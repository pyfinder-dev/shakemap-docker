#!/usr/bin/env bash
# Verify truthful data and readiness reporting through the public HTTP API.
set -euo pipefail

BASE_URL="http://localhost:9010"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) BASE_URL="${2:?--url requires a value}"; shift 2 ;;
        --help|-h) echo "Usage: $0 [--url URL]"; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done
PYTHON_BIN="${SHAKEMAP_HOST_PYTHON:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: Python 3.10 or newer is required; interpreter not found: ${PYTHON_BIN}" >&2
    exit 2
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ERROR: ${PYTHON_BIN} must be Python 3.10 or newer; deployment verification uses only the standard library." >&2
    exit 2
fi

"${PYTHON_BIN}" - "${BASE_URL}" <<'PY'
import json
import sys
import urllib.request

base = sys.argv[1]
responses = {}
for endpoint in ("config", "healthz"):
    with urllib.request.urlopen(f"{base}/{endpoint}", timeout=15) as response:
        responses[endpoint] = json.load(response)
if responses["config"]["identity"] != responses["healthz"]["identity"]:
    raise SystemExit("/config and /healthz image identities differ")
if responses["healthz"]["process_liveness"]["live"] is not True:
    raise SystemExit("/healthz did not report process liveness")
for endpoint in ("config", "healthz"):
    if responses[endpoint]["response_schema_version"] != "1.0":
        raise SystemExit(f"/{endpoint} response schema is not 1.0")
    if responses[endpoint]["managed_execution_readiness"]["ready"] is not False:
        raise SystemExit(f"/{endpoint} misleadingly reports managed calculations ready")
    if responses[endpoint]["overall_readiness"]["ready"] is not False:
        raise SystemExit(f"/{endpoint} misleadingly reports overall readiness")
    summary = responses[endpoint]["data"]["summary"]
    if summary["validation_state"] != "not_evaluated":
        raise SystemExit(f"/{endpoint} conflates inspection with validation")
print(json.dumps({
    "config": {
        "data_summary": responses["config"]["data"]["summary"],
        "managed_execution_readiness": responses["config"]["managed_execution_readiness"],
        "overall_readiness": responses["config"]["overall_readiness"],
    },
    "healthz": {
        "status": responses["healthz"]["status"],
        "process_liveness": responses["healthz"]["process_liveness"],
        "data_summary": responses["healthz"]["data"]["summary"],
        "managed_execution_readiness": responses["healthz"]["managed_execution_readiness"],
        "overall_readiness": responses["healthz"]["overall_readiness"],
    },
    "identity": responses["config"]["identity"],
}, indent=2, sort_keys=True))
PY
