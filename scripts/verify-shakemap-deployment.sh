#!/usr/bin/env bash
# Verify preparation reporting through a running service's public HTTP API.
set -euo pipefail

BASE_URL="http://localhost:9010"
EXPECT="ready"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) BASE_URL="${2:?--url requires a value}"; shift 2 ;;
        --expect) EXPECT="${2:?--expect requires ready or not-ready}"; shift 2 ;;
        --help|-h) echo "Usage: $0 [--url URL] [--expect ready|not-ready]"; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done
if [[ "${EXPECT}" != "ready" && "${EXPECT}" != "not-ready" ]]; then
    echo "ERROR: --expect must be ready or not-ready" >&2
    exit 2
fi
PYTHON_BIN="${SHAKEMAP_HOST_PYTHON:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: Python 3.10 or newer is required; interpreter not found: ${PYTHON_BIN}" >&2
    exit 2
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ERROR: ${PYTHON_BIN} must be Python 3.10 or newer; deployment verification uses only the standard library." >&2
    exit 2
fi

"${PYTHON_BIN}" - "${BASE_URL}" "${EXPECT}" <<'PY'
import json
import sys
import urllib.request

base, expected = sys.argv[1:]
responses = {}
for endpoint in ("config", "healthz"):
    with urllib.request.urlopen(f"{base}/{endpoint}", timeout=15) as response:
        responses[endpoint] = json.load(response)
config_ready = responses["config"]["preparation_readiness"]["ready"]
health_ready = responses["healthz"]["preparation_readiness"]["ready"]
want = expected == "ready"
if config_ready != want or health_ready != want:
    raise SystemExit(f"readiness mismatch: config={config_ready}, healthz={health_ready}, expected={want}")
if responses["config"]["identity"] != responses["healthz"]["identity"]:
    raise SystemExit("/config and /healthz image identities differ")
if responses["healthz"]["process_liveness"]["live"] is not True:
    raise SystemExit("/healthz did not report process liveness")
for endpoint in ("config", "healthz"):
    if responses[endpoint]["managed_calculation_readiness"]["ready"] is not False:
        raise SystemExit(f"/{endpoint} misleadingly reports managed calculations ready")
print(json.dumps({
    "config": {
        "preparation_readiness": responses["config"]["preparation_readiness"],
        "managed_calculation_readiness": responses["config"]["managed_calculation_readiness"],
        "overall_readiness": responses["config"]["overall_readiness"],
    },
    "healthz": {
        "status": responses["healthz"]["status"],
        "process_liveness": responses["healthz"]["process_liveness"],
        "preparation_readiness": {
            "ready": responses["healthz"]["preparation_readiness"]["ready"],
            "state": responses["healthz"]["preparation_readiness"]["state"],
        },
        "managed_calculation_readiness": responses["healthz"]["managed_calculation_readiness"],
        "overall_readiness": responses["healthz"]["overall_readiness"],
    },
    "identity": responses["config"]["identity"],
}, indent=2, sort_keys=True))
PY
