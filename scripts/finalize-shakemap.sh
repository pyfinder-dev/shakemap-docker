#!/usr/bin/env bash
# Prepare, isolate, verify, and publish the canonical deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
source "${SCRIPT_DIR}/container-configuration.sh"

RUNTIME_ROOT="${RUNTIME_ROOT:-${PROJECT_ROOT}/runtime}"
PORT="${PORT:-9010}"
MAX_CONCURRENT="${MAX_CONCURRENT:-10}"
EVENT_ID="shakemap-docker-verification"
BEGUN=0
COMMITTED=0
CURRENT_STEP="initial checks"
SEED_STAGING=""

usage() {
    echo "Usage: $0 [--runtime-root DIR] [--port PORT] [--max-concurrent COUNT]"
}

permission_details() {
    local path="$1"
    local mode
    local owner
    mode="$(stat -f '%Sp' "${path}" 2>/dev/null || stat -c '%A' "${path}" 2>/dev/null || echo unknown)"
    owner="$(stat -f '%u:%g' "${path}" 2>/dev/null || stat -c '%u:%g' "${path}" 2>/dev/null || echo unknown)"
    printf 'mode %s, UID:GID %s' "${mode}" "${owner}"
}

report_permission_failure() {
    local operation="$1"
    local path="$2"
    local result="$3"
    local corrective_action="$4"
    echo "ERROR: ${operation} failed for ${path} with exit code ${result} ($(permission_details "${path}")); ${corrective_action}" >&2
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

fail_closed() {
    local result=$?
    trap - EXIT
    if [[ "${BEGUN}" == 1 && "${COMMITTED}" == 0 ]]; then
        failure_reason="finalization failed during ${CURRENT_STEP} with exit code ${result}; correct the reported error and rerun make finalize"
        failure_recorded=0
        if docker container inspect "${CANONICAL_CONTAINER}" >/dev/null 2>&1 && \
            [[ "$(docker container inspect --format '{{.State.Running}}' "${CANONICAL_CONTAINER}" 2>/dev/null || true)" == "true" ]]; then
            if docker exec "${CANONICAL_CONTAINER}" python -m shakemap_service.finalization fail \
                --reason "${failure_reason}" >/dev/null 2>&1; then
                failure_recorded=1
            fi
        fi
        if [[ "${failure_recorded}" == 0 ]]; then
            RUNTIME_ROOT="${RUNTIME_ABS:-${RUNTIME_ROOT}}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS:-${RUNTIME_ROOT}}" \
                python -m shakemap_service.finalization fail \
                    --reason "${failure_reason}" >/dev/null 2>&1 || true
        fi
        if docker container inspect "${CANONICAL_CONTAINER}" >/dev/null 2>&1 && \
            [[ "$(docker container inspect --format '{{.State.Running}}' "${CANONICAL_CONTAINER}" 2>/dev/null || true)" == "true" ]]; then
            docker stop --time 65 "${CANONICAL_CONTAINER}" >/dev/null 2>&1 || true
        fi
    fi
    if [[ -n "${SEED_STAGING}" && -d "${SEED_STAGING}" ]]; then
        rm -rf -- "${SEED_STAGING}" >/dev/null 2>&1 || true
        SEED_STAGING=""
    fi
    exit "${result}"
}
trap fail_closed EXIT

require_project_environment
validate_deployment_settings
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is not installed." >&2; exit 1; }
mkdir -p "${RUNTIME_ROOT}"
RUNTIME_ABS="$(cd "${RUNTIME_ROOT}" && pwd -P)"
SERVICE_ABS="${RUNTIME_ABS}/shakemap"
load_image_identity

CURRENT_STEP="unfinished-work gate"
RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization begin
BEGUN=1

if docker container inspect "${CANONICAL_CONTAINER}" >/dev/null 2>&1; then
    CURRENT_STEP="existing container mount inspection"
    mount_lines="$(docker container inspect --format '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' "${CANONICAL_CONTAINER}")"
    while IFS='|' read -r source destination; do
        [[ -z "${source}" && -z "${destination}" ]] && continue
        if [[ "${source}" != "${RUNTIME_ABS}" && "${source}" != "${RUNTIME_ABS}/"* ]]; then
            echo "ERROR: canonical container has a mount outside ${RUNTIME_ABS}: ${source}; it was retained." >&2
            exit 1
        fi
        if [[ "${destination}" != "/home/sysop/runtime" && "${destination}" != "/home/sysop/runtime/"* ]]; then
            echo "ERROR: canonical container has an unexpected mount destination: ${destination}; it was retained." >&2
            exit 1
        fi
    done <<<"${mount_lines}"
fi

CURRENT_STEP="graceful service stop"
if docker container inspect "${CANONICAL_CONTAINER}" >/dev/null 2>&1 && \
    [[ "$(docker container inspect --format '{{.State.Running}}' "${CANONICAL_CONTAINER}")" == "true" ]]; then
    docker stop --time 65 "${CANONICAL_CONTAINER}" >/dev/null
fi

CURRENT_STEP="runtime preparation"
RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization prepare-runtime
CURRENT_STEP="staged data activation"
RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization activate-data \
        --data-root "${SERVICE_ABS}/data"
CURRENT_STEP="verification data preparation"
python "${PROJECT_ROOT}/scripts/prepare-shakemap-verification-data.py" prepare \
    --destination "${SERVICE_ABS}/data/test/${IMAGE_VERSION}"

CURRENT_STEP="writable path ownership and access"
writable_roots=(products logs .service data/inputs)
writable_directories=(
    products
    logs
    data/inputs
    .service/events
    .service/archive
    .service/queue
)
writable_special_modes=()
for writable in "${writable_directories[@]}"; do
    path="${SERVICE_ABS}/${writable}"
    if [[ -L "${path}" || ! -d "${path}" ]]; then
        echo "ERROR: service-writable path must be a real directory: ${path}" >&2
        exit 1
    fi
    special=""
    [[ -u "${path}" ]] && special+="u"
    [[ -g "${path}" ]] && special+="g"
    [[ -k "${path}" ]] && special+="t"
    writable_special_modes+=("${special}")
done

# Ownership changes can clear directory special bits even when a recursive
# change fails partway, so restoration always covers every captured directory.
restore_writable_special_modes() {
    local first_result=0
    local index
    local path
    local special
    local result
    local mode
    local operation
    for index in "${!writable_directories[@]}"; do
        path="${SERVICE_ABS}/${writable_directories[index]}"
        special="${writable_special_modes[index]}"
        for mode in u g t; do
            [[ "${special}" == *"${mode}"* ]] || continue
            if [[ "${mode}" == t ]]; then
                operation="chmod +t"
            else
                operation="chmod ${mode}+s"
            fi
            if chmod "${operation#chmod }" "${path}"; then
                continue
            else
                result=$?
            fi
            report_permission_failure "${operation}" "${path}" "${result}" \
                "restore this mode as the path owner or with sufficient host permission, then rerun finalization."
            if [[ "${first_result}" == 0 ]]; then
                first_result="${result}"
            fi
        done
    done
    return "${first_result}"
}

for writable in "${writable_roots[@]}"; do
    path="${SERVICE_ABS}/${writable}"
    if chown -R 1000:1000 "${path}" 2>/dev/null; then
        continue
    else
        result=$?
    fi
    report_permission_failure "chown -R 1000:1000" "${path}" "${result}" \
        "rerun finalization as a host user permitted to assign UID:GID 1000:1000."
    restore_writable_special_modes || true
    exit "${result}"
done
# Only the directories used for service writes need additional mode bits.
# Symbolic additions leave group/other and every existing ordinary bit intact.
for index in "${!writable_directories[@]}"; do
    path="${SERVICE_ABS}/${writable_directories[index]}"
    if chmod u+rwx "${path}"; then
        continue
    else
        result=$?
    fi
    report_permission_failure "chmod u+rwx" "${path}" "${result}" \
        "rerun finalization as the path owner or with sufficient host permission."
    restore_writable_special_modes || true
    exit "${result}"
done
if restore_writable_special_modes; then
    :
else
    result=$?
    exit "${result}"
fi

CURRENT_STEP="isolated canonical container creation"
if docker container inspect "${CANONICAL_CONTAINER}" >/dev/null 2>&1; then
    docker rm "${CANONICAL_CONTAINER}" >/dev/null
fi
resolve_runtime_root
container_create_command isolated
"${CONTAINER_COMMAND[@]}" >/dev/null
CURRENT_STEP="image regional seed extraction"
SEED_STAGING="$(mktemp -d "${TMPDIR:-/tmp}/shakemap-regional-seeds.XXXXXX")"
docker cp "${CANONICAL_CONTAINER}:/opt/shakemap-seeds/regional/." "${SEED_STAGING}/"
RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization prepare-runtime \
        --regional-seeds "${SEED_STAGING}"
rm -rf -- "${SEED_STAGING}"
SEED_STAGING=""
RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization arm --image-id "${IMAGE_ID}"
CURRENT_STEP="isolated canonical container start"
docker start "${CANONICAL_CONTAINER}" >/dev/null
wait_for_container_health true
CURRENT_STEP="container-internal checks"
docker exec "${CANONICAL_CONTAINER}" /app/scripts/verify-shakemap-image.sh --deployment

CURRENT_STEP="isolated verification calculation"
submission="$(docker exec "${CANONICAL_CONTAINER}" shake-in-docker \
    --url http://127.0.0.1:9010 submit "${EVENT_ID}" \
    --configuration global --overwrite true \
    --file /opt/shakemap-verification/event.xml \
    --file /opt/shakemap-verification/event_dat.xml)"
sequence="$(python -c 'import json,sys; print(json.load(sys.stdin)["internal_sequence"])' <<<"${submission}")"
docker exec "${CANONICAL_CONTAINER}" python - "${EVENT_ID}" "${sequence}" <<'PY'
import json
import sys
import time
import urllib.parse
import urllib.request

event_id, sequence = sys.argv[1], int(sys.argv[2])
base = "http://127.0.0.1:9010/events/" + urllib.parse.quote(event_id, safe="")
deadline = time.monotonic() + 1800
while time.monotonic() < deadline:
    with urllib.request.urlopen(base, timeout=10) as response:
        detail = json.load(response)
    current = next((job for job in detail["jobs"] if job["internal_sequence"] == sequence), None)
    if current and current["status"] in {"SUCCESS", "FAILED"}:
        break
    time.sleep(2)
else:
    raise SystemExit("verification calculation did not reach a terminal state")
if current["status"] != "SUCCESS" or current["job_completed"] is not True or current["products_ready"] is not True:
    raise SystemExit("verification calculation did not satisfy the SUCCESS gate")
with urllib.request.urlopen(base + "/products", timeout=10) as response:
    products = json.load(response)["current"]
if products["internal_sequence"] != sequence or products["status"] != "SUCCESS" or products["products_ready"] is not True:
    raise SystemExit("verification product summary does not match the successful sequence")
PY

CURRENT_STEP="published canonical container recreation"
docker stop --time 65 "${CANONICAL_CONTAINER}" >/dev/null
docker rm "${CANONICAL_CONTAINER}" >/dev/null
container_run_command published
"${CONTAINER_COMMAND[@]}" >/dev/null
wait_for_container_health false

BASE_URL="http://127.0.0.1:${PORT}"
CURRENT_STEP="pre-ready public parity checks"
rest_health="$(python - "${BASE_URL}" <<'PY'
import json,sys,urllib.request
with urllib.request.urlopen(sys.argv[1]+"/healthz", timeout=30) as response: print(json.dumps(json.load(response), sort_keys=True))
PY
)"
cli_health="$("${PROJECT_CLI}" --url "${BASE_URL}" health)"
rest_status="$(python - "${BASE_URL}" "${EVENT_ID}" <<'PY'
import json,sys,urllib.parse,urllib.request
url=sys.argv[1]+"/events/"+urllib.parse.quote(sys.argv[2], safe="")
with urllib.request.urlopen(url, timeout=30) as response: print(json.dumps(json.load(response), sort_keys=True))
PY
)"
cli_status="$("${PROJECT_CLI}" --url "${BASE_URL}" status "${EVENT_ID}")"
rest_products="$(python - "${BASE_URL}" "${EVENT_ID}" <<'PY'
import json,sys,urllib.parse,urllib.request
url=sys.argv[1]+"/events/"+urllib.parse.quote(sys.argv[2], safe="")+"/products"
with urllib.request.urlopen(url, timeout=30) as response: print(json.dumps(json.load(response), sort_keys=True))
PY
)"
cli_products="$("${PROJECT_CLI}" --url "${BASE_URL}" products "${EVENT_ID}")"
python - "${sequence}" "${rest_health}" "${cli_health}" "${rest_status}" "${cli_status}" "${rest_products}" "${cli_products}" <<'PY'
import json,sys
sequence=int(sys.argv[1]); values=[json.loads(value) for value in sys.argv[2:]]
if len(values) != 6 or values[0] != values[1] or values[2] != values[3] or values[4] != values[5]:
    raise SystemExit("REST and CLI retained verification views differ")
if values[0].get("ready") is not False:
    raise SystemExit("deployment became ready before final parity checks completed")
if not any(job.get("internal_sequence") == sequence and job.get("status") == "SUCCESS" for job in values[2]["jobs"]):
    raise SystemExit("retained event detail does not contain the successful sequence")
if values[4]["current"].get("internal_sequence") != sequence:
    raise SystemExit("retained product summary identifies another sequence")
PY

CURRENT_STEP="durable readiness publication"
docker exec "${CANONICAL_CONTAINER}" python -m shakemap_service.finalization ready
wait_for_container_health true
COMMITTED=1
trap - EXIT
echo "Canonical ShakeMap deployment finalized and ready at ${BASE_URL}."
