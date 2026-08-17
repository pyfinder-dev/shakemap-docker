#!/usr/bin/env bash
# Start only the matching canonical finalized deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
source "${SCRIPT_DIR}/container-configuration.sh"

RUNTIME_ROOT="${RUNTIME_ROOT:-${PROJECT_ROOT}/runtime}"
PORT="${PORT:-9010}"
MAX_CONCURRENT="${MAX_CONCURRENT:-10}"

usage() {
    echo "Usage: $0 [--runtime-root DIR] [--port PORT] [--max-concurrent COUNT]"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime-root) RUNTIME_ROOT="${2:?--runtime-root requires a directory}"; shift 2 ;;
        --port) PORT="${2:?--port requires a value}"; shift 2 ;;
        --max-concurrent) MAX_CONCURRENT="${2:?--max-concurrent requires a value}"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

require_project_environment
validate_deployment_settings
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is not installed." >&2; exit 1; }
resolve_runtime_root
load_image_identity

if ! RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization check-ready \
        --image-id "${IMAGE_ID}" --release-tag "${IMAGE_RELEASE}" \
        --source-commit "${IMAGE_COMMIT}" --shakemap-version "${IMAGE_VERSION}"; then
    echo "ERROR: deployment readiness does not match the canonical image; run make finalize." >&2
    exit 1
fi

presence="$(canonical_container_presence)"
if [[ "${presence}" == "present" ]]; then
    if ! verify_canonical_container_configuration published; then
        echo "ERROR: run make finalize to recreate the canonical container." >&2
        exit 1
    fi
    if [[ "$(docker container inspect --format '{{.State.Running}}' "${CANONICAL_CONTAINER}")" != "true" ]]; then
        docker start "${CANONICAL_CONTAINER}" >/dev/null
    fi
elif [[ "${presence}" == "absent" ]]; then
    container_run_command published
    "${CONTAINER_COMMAND[@]}" >/dev/null
else
    echo "ERROR: canonical container presence is unavailable." >&2
    exit 1
fi

BASE_URL="http://127.0.0.1:${PORT}"
for attempt in $(seq 1 60); do
    if health="$("${PROJECT_CLI}" --url "${BASE_URL}" health 2>/dev/null)" && \
        python -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ready") is True else 1)' <<<"${health}"; then
        echo "Canonical ShakeMap service is running and ready at ${BASE_URL}."
        exit 0
    fi
    sleep 1
done
echo "ERROR: canonical service did not become ready; run make finalize." >&2
exit 1
