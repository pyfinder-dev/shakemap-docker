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

python - "${BASE_URL}" "${EXPECT}" <<'PY'
import json
import sys
import urllib.request

base, expected = sys.argv[1:]
responses = {}
for endpoint in ("config", "healthz"):
    with urllib.request.urlopen(f"{base}/{endpoint}", timeout=15) as response:
        responses[endpoint] = json.load(response)
config_ready = responses["config"]["scientific_readiness"]["ready"]
health_ready = responses["healthz"]["scientific_readiness"]["ready"]
want = expected == "ready"
if config_ready != want or health_ready != want:
    raise SystemExit(f"readiness mismatch: config={config_ready}, healthz={health_ready}, expected={want}")
if responses["config"]["identity"] != responses["healthz"]["identity"]:
    raise SystemExit("/config and /healthz image identities differ")
print(json.dumps(responses, indent=2, sort_keys=True))
PY
