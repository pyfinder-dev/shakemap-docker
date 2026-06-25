#!/usr/bin/env bash
set -uo pipefail

# ------------------------------------------------------------------
# verify-shakemap-deployment.sh -- Deployment Verification
#
# Verifies a RUNNING container without rebuilding images or creating
# disposable containers.  Supports two modes:
#
#   --expect not-ready   After start, before configure
#   --expect ready       After configure
#
# Usage:
#   ./scripts/verify-shakemap-deployment.sh <container> --expect ready
#   ./scripts/verify-shakemap-deployment.sh <container> --expect not-ready
#
# Exit code: 0 if all checks pass, 1 if any fail.
# ------------------------------------------------------------------

# -- Parse arguments --
CONTAINER=""
EXPECT_MODE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --expect)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --expect requires a value: ready or not-ready" >&2
                exit 1
            fi
            EXPECT_MODE="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 <container-name-or-id> --expect ready|not-ready"
            exit 0
            ;;
        -*)
            echo "ERROR: Unknown option: $1" >&2
            exit 1
            ;;
        *)
            if [[ -z "${CONTAINER}" ]]; then
                CONTAINER="$1"
            else
                echo "ERROR: Unexpected argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "${CONTAINER}" ]]; then
    echo "ERROR: Container name or ID is required." >&2
    echo "Usage: $0 <container-name-or-id> --expect ready|not-ready" >&2
    exit 1
fi

if [[ "${EXPECT_MODE}" != "ready" && "${EXPECT_MODE}" != "not-ready" ]]; then
    echo "ERROR: --expect must be 'ready' or 'not-ready' (got: '${EXPECT_MODE}')" >&2
    exit 1
fi

PASS=0
FAIL=0
TOTAL=0

check() {
    local label="$1"
    local result="$2"
    TOTAL=$((TOTAL + 1))
    if [ "${result}" = "0" ]; then
        echo "  PASS  D-${TOTAL}: ${label}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  D-${TOTAL}: ${label}"
        FAIL=$((FAIL + 1))
    fi
}

echo "===== Deployment Verification (expect: ${EXPECT_MODE}) ====="
echo "  Container: ${CONTAINER}"
echo ""

# -- Common checks (both modes) --

# D-1: Container is running
RUNNING="$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo "false")"
[ "${RUNNING}" = "true" ]; check "Container is running" $?

if [ "${RUNNING}" != "true" ]; then
    echo ""
    echo "ERROR: Container '${CONTAINER}' is not running. Cannot continue."
    exit 1
fi

# D-2: Scripts exist inside container
SCRIPTS_OK=0
for script in configure-shakemap.sh build-shakemap-docker.sh verify-shakemap-build.sh verify-shakemap-config.sh; do
    if ! docker exec "${CONTAINER}" test -f "/app/scripts/${script}" 2>/dev/null; then
        SCRIPTS_OK=1
        echo "         Missing: /app/scripts/${script}"
    fi
done
check "Required scripts exist inside container" ${SCRIPTS_OK}

# D-3: User is sysop
CUSER="$(docker exec "${CONTAINER}" id -un 2>/dev/null || echo "unknown")"
[ "${CUSER}" = "sysop" ]; check "User is sysop (got: ${CUSER})" $?

# D-4: UID is 1000
CUID="$(docker exec "${CONTAINER}" id -u 2>/dev/null || echo "-1")"
[ "${CUID}" = "1000" ]; check "UID is 1000 (got: ${CUID})" $?

# D-5: Runtime dirs exist and writable
DIRS_OK=0
DIR_CHECK="$(docker exec "${CONTAINER}" bash -c '
    SERVICE_ROOT="${SERVICE_ROOT:-/home/sysop/runtime/shakemap}"
    for dir in events incoming work products archive logs; do
        DIRPATH="${SERVICE_ROOT}/${dir}"
        if [ ! -d "${DIRPATH}" ]; then
            echo "MISSING:${dir}"
        elif ! touch "${DIRPATH}/.deploycheck_$$" 2>/dev/null; then
            echo "NOWRITE:${dir}"
        else
            rm -f "${DIRPATH}/.deploycheck_$$"
        fi
    done
' 2>/dev/null)"
if echo "${DIR_CHECK}" | grep -qE "MISSING:|NOWRITE:"; then
    DIRS_OK=1
    echo "         ${DIR_CHECK}"
fi
check "Runtime directories exist and writable" ${DIRS_OK}

# D-6: ShakeMap CLI on PATH
SHAKE_ON_PATH="$(docker exec "${CONTAINER}" command -v shake 2>/dev/null)"
[ -n "${SHAKE_ON_PATH}" ]; check "ShakeMap CLI (shake) on PATH" $?

# D-7: /healthz responds with JSON
HEALTHZ_JSON="$(docker exec "${CONTAINER}" python3 -c "
import json, urllib.request
try:
    with urllib.request.urlopen('http://localhost:9010/healthz', timeout=10) as r:
        data = json.loads(r.read().decode())
        print(json.dumps(data))
except Exception as e:
    print('ERROR:' + str(e))
" 2>/dev/null)"
HEALTHZ_OK=1
if echo "${HEALTHZ_JSON}" | python3 -c "import sys,json; json.loads(sys.stdin.read())" 2>/dev/null; then
    HEALTHZ_OK=0
fi
check "/healthz responds with valid JSON" ${HEALTHZ_OK}

# D-8: Image identifier/digest
IMAGE_ID="$(docker inspect -f '{{.Image}}' "${CONTAINER}" 2>/dev/null || echo "unknown")"
IMAGE_NAME="$(docker inspect -f '{{.Config.Image}}' "${CONTAINER}" 2>/dev/null || echo "unknown")"
echo "  INFO  Image: ${IMAGE_NAME}"
echo "  INFO  Image ID: ${IMAGE_ID}"
[ "${IMAGE_ID}" != "unknown" ]; check "Image identifier available" $?

# -- Mode-specific checks --
echo ""

if [ "${EXPECT_MODE}" = "not-ready" ]; then
    echo "--- Checks for --expect not-ready ---"

    # Parse healthz response
    MODE_RESULT="$(docker exec "${CONTAINER}" python3 - <<'PYEOF'
import json, urllib.request, urllib.error

try:
    with urllib.request.urlopen("http://localhost:9010/healthz", timeout=10) as r:
        data = json.loads(r.read().decode())
        status = data.get("status", "")
        s1 = str(data.get("stage1", {}).get("passed", False)).lower()
        s2 = str(data.get("stage2", {}).get("passed", False)).lower()
except Exception:
    status = ""
    s1 = "false"
    s2 = "true"

# Submit gate
submit_code = "0"
try:
    boundary = "----DeployCheck"
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"event_id\"\r\n\r\ndeploy_check")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"user_id\"\r\n\r\ntest")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"t.txt\"\r\nContent-Type: text/plain\r\n\r\ntest")
    parts.append(f"--{boundary}--\r\n")
    body = "\r\n".join(parts).encode()
    req = urllib.request.Request(
        "http://localhost:9010/events/submit",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        submit_code = str(resp.status)
except urllib.error.HTTPError as e:
    submit_code = str(e.code)
except Exception:
    pass

print(f"{status}|{s1}|{s2}|{submit_code}")
PYEOF
    )"

    IFS='|' read -r STATUS S1_PASSED S2_PASSED SUBMIT_CODE <<< "${MODE_RESULT}"

    [ "${STATUS}" = "not_ready" ]; check "/healthz status == 'not_ready'" $?
    [ "${S1_PASSED}" = "true" ]; check "/healthz stage1.passed == true" $?
    [ "${S2_PASSED}" = "false" ]; check "/healthz stage2.passed == false" $?
    [ "${SUBMIT_CODE}" = "503" ]; check "POST /events/submit returns HTTP 503" $?

elif [ "${EXPECT_MODE}" = "ready" ]; then
    echo "--- Checks for --expect ready ---"

    MODE_RESULT="$(docker exec "${CONTAINER}" python3 - <<'PYEOF'
import json, urllib.request, urllib.error

try:
    with urllib.request.urlopen("http://localhost:9010/healthz", timeout=10) as r:
        data = json.loads(r.read().decode())
        status = data.get("status", "")
        s1 = str(data.get("stage1", {}).get("passed", False)).lower()
        s2 = str(data.get("stage2", {}).get("passed", False)).lower()
except Exception:
    status = ""
    s1 = "false"
    s2 = "false"

# Submit gate
submit_code = "0"
try:
    boundary = "----DeployCheck"
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"event_id\"\r\n\r\ndeploy_check")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"user_id\"\r\n\r\ntest")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"t.txt\"\r\nContent-Type: text/plain\r\n\r\ntest")
    parts.append(f"--{boundary}--\r\n")
    body = "\r\n".join(parts).encode()
    req = urllib.request.Request(
        "http://localhost:9010/events/submit",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        submit_code = str(resp.status)
except urllib.error.HTTPError as e:
    submit_code = str(e.code)
except Exception:
    pass

# Sentinel
sentinel_ok = "false"
try:
    import os
    home = os.environ.get("HOME", "/home/sysop")
    spath = os.path.join(home, ".shakemap", ".shakemap_readiness_status")
    if os.path.isfile(spath):
        with open(spath) as f:
            content = f.read().strip()
        if content.startswith("ready"):
            sentinel_ok = "true"
except Exception:
    pass

print(f"{status}|{s1}|{s2}|{submit_code}|{sentinel_ok}")
PYEOF
    )"

    IFS='|' read -r STATUS S1_PASSED S2_PASSED SUBMIT_CODE SENTINEL_OK <<< "${MODE_RESULT}"

    if [ "${STATUS}" = "healthy" ] || [ "${STATUS}" = "healthy_with_overrides" ]; then
        check "/healthz status is healthy or healthy_with_overrides (got: ${STATUS})" 0
        if [ "${STATUS}" = "healthy_with_overrides" ]; then
            echo "  WARNING: Container is running with active overrides (e.g. uniform VS30)."
            echo "           This is not a fully-provisioned production installation."
        fi
    else
        check "/healthz status is healthy or healthy_with_overrides (got: ${STATUS})" 1
    fi
    [ "${S1_PASSED}" = "true" ]; check "/healthz stage1.passed == true" $?
    [ "${S2_PASSED}" = "true" ]; check "/healthz stage2.passed == true" $?

    GATE_OK=1
    if [ "${SUBMIT_CODE}" != "503" ] && [ "${SUBMIT_CODE}" != "0" ]; then
        GATE_OK=0
    fi
    check "POST /events/submit does NOT return 503 (got: ${SUBMIT_CODE})" ${GATE_OK}
    [ "${SENTINEL_OK}" = "true" ]; check "Readiness sentinel present and says 'ready'" $?
fi

# -- Summary --
echo ""
echo "===== Deployment Verification: ${PASS} passed, ${FAIL} failed (of ${TOTAL}) ====="

if [ ${FAIL} -gt 0 ]; then
    exit 1
else
    exit 0
fi
