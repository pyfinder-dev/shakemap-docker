#!/usr/bin/env bash
# Prepare, isolate, verify, and publish the canonical deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
source "${SCRIPT_DIR}/container-configuration.sh"
ORIGINAL_ARGUMENTS=("$@")

RUNTIME_ROOT="${RUNTIME_ROOT:-${PROJECT_ROOT}/runtime}"
PORT="${PORT:-9010}"
MAX_CONCURRENT="${MAX_CONCURRENT:-10}"
EVENT_ID="shakemap-docker-verification"
BEGUN=0
COMMITTED=0
CURRENT_STEP="initial checks"
SEED_STAGING=""
OWNED_CONTAINER_ID=""

usage() {
    echo "Usage: $0 [--runtime-root DIR] [--port PORT] [--max-concurrent COUNT]"
}

manual_repair_guidance() {
    echo "For confirmed service-writable permission failures only: UID:GID 1000:1000 needs access. Wait for accepted work to finish, then stop the service and all runtime writers before manual repair." >&2
    printf 'Run manually: sudo %q --runtime-root %q\n' \
        "${SCRIPT_DIR}/repair-shakemap-writable-paths.sh" "${RUNTIME_ABS:-${RUNTIME_ROOT}}" >&2
    echo "Then rerun finalization normally. Do not run the entire finalizer with sudo." >&2
}

cleanup_seed_staging() {
    [[ -n "${SEED_STAGING}" && -d "${SEED_STAGING}" && ! -L "${SEED_STAGING}" ]] || return 0
    # An interrupted archive can leave read-only directories. Restore owner
    # traversal/removal access only inside our disposable tree, without following
    # links or changing files that might be hard-linked outside that tree.
    find -P "${SEED_STAGING}" -type d -exec chmod u+rwx {} \; || return
    rm -rf -- "${SEED_STAGING}" || return
    SEED_STAGING=""
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
        if [[ -n "${OWNED_CONTAINER_ID}" ]] && \
            [[ "$(docker container inspect --format '{{.State.Running}}' "${OWNED_CONTAINER_ID}" 2>/dev/null || true)" == "true" ]]; then
            if docker exec "${OWNED_CONTAINER_ID}" python -m shakemap_service.finalization fail \
                --reason "${failure_reason}" >/dev/null 2>&1; then
                failure_recorded=1
            fi
        fi
        if [[ "${failure_recorded}" == 0 ]]; then
            RUNTIME_ROOT="${RUNTIME_ABS:-${RUNTIME_ROOT}}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS:-${RUNTIME_ROOT}}" \
                python -m shakemap_service.finalization fail \
                    --reason "${failure_reason}" >/dev/null 2>&1 || true
        fi
        if [[ -n "${OWNED_CONTAINER_ID}" ]] && \
            [[ "$(docker container inspect --format '{{.State.Running}}' "${OWNED_CONTAINER_ID}" 2>/dev/null || true)" == "true" ]]; then
            docker stop --time 65 "${OWNED_CONTAINER_ID}" >/dev/null 2>&1 || true
        fi
    fi
    cleanup_seed_staging || echo "ERROR: could not remove seed staging at ${SEED_STAGING}; inspect the reported cleanup error." >&2
    exit "${result}"
}
trap fail_closed EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_project_environment
validate_deployment_settings
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is not installed." >&2; exit 1; }
acquire_finalization_lock "${SCRIPT_DIR}/finalize-shakemap.sh" "${CANONICAL_CONTAINER}" ${ORIGINAL_ARGUMENTS[@]+"${ORIGINAL_ARGUMENTS[@]}"}
RUNTIME_ABS="$(python - finalization-runtime "${RUNTIME_ROOT}" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[2]).resolve())
PY
)"
SERVICE_ABS="${RUNTIME_ABS}/shakemap"
CURRENT_STEP="existing container mount inspection"
presence="$(canonical_container_presence)"
if [[ "${presence}" == present ]]; then
    OWNED_CONTAINER_ID="$(inspect_finalization_mounts)"
fi
# Existing mount ownership is known before creating a first-install runtime or
# changing either deployment's readiness. Old images and stopped probes remain
# eligible for finalization; readiness/settings checks belong to normal start.
mkdir -p "${RUNTIME_ABS}"
load_image_identity

CURRENT_STEP="unfinished-work gate"
if RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization begin; then :; else
    result=$?
    # The Python operation retains errno and the exact failed path through
    # wrappers, so it owns recovery advice for these early failures.
    exit "${result}"
fi
BEGUN=1

CURRENT_STEP="graceful service stop"
if [[ -n "${OWNED_CONTAINER_ID}" ]] && \
    [[ "$(docker container inspect --format '{{.State.Running}}' "${OWNED_CONTAINER_ID}")" == "true" ]]; then
    docker stop --time 65 "${OWNED_CONTAINER_ID}" >/dev/null
fi

CURRENT_STEP="runtime preparation"
if RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization prepare-runtime; then :; else
    result=$?
    exit "${result}"
fi
CURRENT_STEP="staged data activation"
RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization activate-data \
        --data-root "${SERVICE_ABS}/data"
CURRENT_STEP="verification data preparation"
python "${PROJECT_ROOT}/scripts/prepare-shakemap-verification-data.py" prepare \
    --destination "${SERVICE_ABS}/data/test/${IMAGE_VERSION}"

CURRENT_STEP="container writable access probe"
# Replace the stopped canonical container only after its mounts and unfinished
# work were checked. The probe overrides the entrypoint, never runs the API,
# and retains its stopped container on failure for inspection.
resolve_runtime_root
probe_code="$(cat <<'PY'
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile

if (os.getuid(), os.getgid()) != (1000, 1000):
    raise SystemExit("ERROR: writable probe requires UID:GID 1000:1000")
root = Path('/home/sysop/runtime/shakemap')
host = Path(sys.argv[1])
failed = False
non_permission_failure = False
def traversal_error(error):
    raise error

for relative in ('products', 'logs', 'data/inputs', '.service', '.service/events', '.service/archive', '.service/queue'):
    path = root / relative
    report_path = path
    temporary = None
    operation = 'inspect directory'
    try:
        if path.is_symlink() or not path.is_dir():
            raise OSError('expected real directory')
        operation = 'create/write/rename/delete'
        fd, name = tempfile.mkstemp(prefix='.shakemap-write-probe-', dir=path)
        temporary = Path(name)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(b'write-access-probe')
            stream.flush()
        renamed = temporary.with_name(temporary.name + '.renamed')
        temporary.rename(renamed)
        temporary = renamed
        temporary.unlink()
        temporary = None
        # Opening service state read/write proves existing lock/state access
        # without truncating, acquiring locks, or changing its content.
        if relative == '.service':
            for directory, directories, files in os.walk(path, onerror=traversal_error, followlinks=False):
                for name in directories + files:
                    state = Path(directory) / name
                    report_path = state
                    operation = f'inspect existing state {state.relative_to(root)}'
                    if state.is_symlink():
                        raise OSError(f'symbolic service state: {state}')
                    parts = state.relative_to(path).parts
                    native_cache = (
                        len(parts) == 4 and parts[0] == 'events'
                        and parts[2:] == ('profile', 'cache')
                    ) or (
                        len(parts) == 5 and parts[0] == 'archive'
                        and parts[2:] == ('service', 'profile', 'cache')
                    )
                    if native_cache:
                        if name not in directories or not stat.S_ISDIR(state.lstat().st_mode):
                            raise OSError(f'expected real native cache directory: {state}')
                        # Native libraries own disposable cache contents, which
                        # may contain links. Check the real boundary above,
                        # then prune it without reading or repairing children.
                        directories.remove(name)
                        continue
                    if name in files:
                        operation = f'open existing state {state.relative_to(root)}'
                        if not stat.S_ISREG(state.lstat().st_mode):
                            raise OSError(f'non-regular service state: {state}')
                        fd = os.open(state, os.O_RDWR | os.O_NOFOLLOW)
                        os.close(fd)
    except OSError as exc:
        failed = True
        # Only access-denied errors justify ownership repair. Storage, layout,
        # and mount failures must be corrected without changing host ownership.
        if exc.errno not in (errno.EACCES, errno.EPERM):
            non_permission_failure = True
        try:
            info = report_path.lstat()
            details = f'mode {stat.filemode(info.st_mode)}, UID:GID {info.st_uid}:{info.st_gid}'
        except OSError:
            details = 'mode/owner unavailable'
        print(f'ERROR: {operation} at {host / report_path.relative_to(root)} ({details}); service UID:GID 1000:1000: {exc}', file=sys.stderr)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError as exc:
                failed = True
                non_permission_failure = True
                print(f'ERROR: probe cleanup at {host / temporary.relative_to(root)}: {exc}', file=sys.stderr)
raise SystemExit(1 if non_permission_failure else 73 if failed else 0)
PY
)"
probe_writable_access() {
    local result
    local state
    if [[ -n "${OWNED_CONTAINER_ID}" ]]; then
        docker rm "${OWNED_CONTAINER_ID}" >/dev/null || return 1
        OWNED_CONTAINER_ID=""
    fi
    container_create_command probe
    CONTAINER_COMMAND+=(-c "${probe_code}" "${SERVICE_ABS}")
    created_container_id="$("${CONTAINER_COMMAND[@]}")" || return 1
    [[ "${created_container_id}" =~ ^[0-9a-f]{64}$ ]] || { echo "ERROR: created probe identity is unavailable." >&2; return 1; }
    OWNED_CONTAINER_ID="${created_container_id}"
    verify_canonical_container_configuration probe || return 1
    result=0
    docker start -a "${CANONICAL_CONTAINER}" || result=$?
    state="$(docker container inspect --format '{{.State.Status}}:{{.State.ExitCode}}:{{.State.Error}}' "${CANONICAL_CONTAINER}")" || return 1
    # Only our explicit access-error exit code authorizes host repair. Docker
    # creation/start/inspection failures must never become ownership changes.
    if [[ "${state}" == exited:73: && ( "${result}" == 0 || "${result}" == 73 ) ]]; then return 73; fi
    if [[ "${state}" == exited:0: && "${result}" == 0 ]]; then return 0; fi
    echo "ERROR: writable probe execution failed (${state}, docker exit ${result}); inspect the retained canonical container and Docker before retrying." >&2
    return 1
}
if probe_writable_access; then :; else
    result=$?
    [[ "${result}" == 73 ]] || exit "${result}"
    CURRENT_STEP="service writable path repair"
    if "${SCRIPT_DIR}/repair-shakemap-writable-paths.sh" --runtime-root "${RUNTIME_ABS}"; then :; else
        result=$?
        # A failed shell utility does not prove missing privilege. Preserve the
        # helper's operation-specific diagnostic and stop before any reprobe.
        exit "${result}"
    fi
    CURRENT_STEP="container writable access reprobe"
    if probe_writable_access; then :; else
        result=$?
        [[ "${result}" != 73 ]] || manual_repair_guidance
        exit "${result}"
    fi
fi

CURRENT_STEP="isolated canonical container creation"
if [[ -n "${OWNED_CONTAINER_ID}" ]]; then
    docker rm "${OWNED_CONTAINER_ID}" >/dev/null
    OWNED_CONTAINER_ID=""
fi
resolve_runtime_root
container_create_command isolated
created_container_id="$("${CONTAINER_COMMAND[@]}")"
[[ "${created_container_id}" =~ ^[0-9a-f]{64}$ ]] || { echo "ERROR: created isolated container identity is unavailable." >&2; exit 1; }
OWNED_CONTAINER_ID="${created_container_id}"
CURRENT_STEP="image regional seed extraction"
SEED_STAGING="$(mktemp -d "${TMPDIR:-/tmp}/shakemap-regional-seeds.XXXXXX")"
# Host tar delays directory-mode restoration until their children are extracted.
# pipefail keeps a failed Docker producer from being hidden by a successful tar.
docker cp "${CANONICAL_CONTAINER}:/opt/shakemap-seeds/regional/." - \
    | tar -xf - --no-same-owner -C "${SEED_STAGING}"
# Only real directories and singly-linked regular files may be normalized and
# copied. Image seeds stay immutable; missing-only publication copies these
# owner-editable staging modes without touching existing operator configs.
unsafe_seed="$(find -P "${SEED_STAGING}" \( \( ! -type d -a ! -type f \) -o \( -type f -a -links +1 \) \) -print)"
if [[ -n "${unsafe_seed}" ]]; then
    printf 'ERROR: unsafe regional seed entries in disposable staging:\n%s\n' "${unsafe_seed}" >&2
    exit 2
fi
chmod -R u+rwX "${SEED_STAGING}"
RUNTIME_ROOT="${RUNTIME_ABS}" SHAKEMAP_SHARED_RUNTIME_ROOT="${RUNTIME_ABS}" \
    python -m shakemap_service.finalization prepare-runtime \
        --regional-seeds "${SEED_STAGING}"
cleanup_seed_staging
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
# Keep stdin attached so the terminal-state gate executes before publication.
docker exec -i "${CANONICAL_CONTAINER}" python - "${EVENT_ID}" "${sequence}" <<'PY'
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
OWNED_CONTAINER_ID=""
container_run_command published
created_container_id="$("${CONTAINER_COMMAND[@]}")"
[[ "${created_container_id}" =~ ^[0-9a-f]{64}$ ]] || { echo "ERROR: created published container identity is unavailable." >&2; exit 1; }
OWNED_CONTAINER_ID="${created_container_id}"
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
