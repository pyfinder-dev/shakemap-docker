#!/usr/bin/env bash
set -uo pipefail

# ------------------------------------------------------------------
# verify-shakemap-build.sh -- Build / Infrastructure Verification
#
# Validates all Stage 1 guarantees (plan S12.1, V1-1 through V1-14).
# Run inside the container after docker run, before configure.
#
# Usage:
#   docker exec <container> /app/scripts/verify-shakemap-build.sh
#
# Exit code: 0 if all checks pass, 1 if any fail.
# ------------------------------------------------------------------

PASS=0
FAIL=0
TOTAL=0

check() {
    local label="$1"
    local result="$2"
    TOTAL=$((TOTAL + 1))
    if [ "${result}" = "0" ]; then
        echo "  PASS  V1-${TOTAL}: ${label}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  V1-${TOTAL}: ${label}"
        FAIL=$((FAIL + 1))
    fi
}

RUNTIME_ROOT="${RUNTIME_ROOT:-/home/sysop/runtime}"
SERVICE_ROOT="${SERVICE_ROOT:-${RUNTIME_ROOT}/shakemap}"
PORT="${SHAKEMAP_PORT:-9010}"

echo "===== Stage 1 Verification ====="
echo ""

# V1-1: Container user is sysop
CURRENT_USER="$(id -un 2>/dev/null || echo unknown)"
[ "${CURRENT_USER}" = "sysop" ]; check "User is sysop (got: ${CURRENT_USER})" $?

# V1-2: UID is 1000
CURRENT_UID="$(id -u 2>/dev/null || echo -1)"
[ "${CURRENT_UID}" = "1000" ]; check "UID is 1000 (got: ${CURRENT_UID})" $?

# V1-3: GID is 1000
CURRENT_GID="$(id -g 2>/dev/null || echo -1)"
[ "${CURRENT_GID}" = "1000" ]; check "GID is 1000 (got: ${CURRENT_GID})" $?

# V1-4: Runtime root exists
[ -d "${RUNTIME_ROOT}" ]; check "Runtime root exists: ${RUNTIME_ROOT}" $?

# V1-5: Service root exists
[ -d "${SERVICE_ROOT}" ]; check "Service root exists: ${SERVICE_ROOT}" $?

# V1-6: All contract directories exist (user-facing + .service/ internal)
ALL_DIRS_OK=0
for dir in incoming products logs data .service .service/events .service/work .service/archive; do
    if [ ! -d "${SERVICE_ROOT}/${dir}" ]; then
        ALL_DIRS_OK=1
        echo "         Missing: ${SERVICE_ROOT}/${dir}"
    fi
done
check "All contract directories exist" ${ALL_DIRS_OK}

# V1-7: All writable directories are writable
ALL_WRITABLE=0
for dir in incoming products logs .service/events .service/work .service/archive; do
    TESTFILE="${SERVICE_ROOT}/${dir}/.writetest_$$"
    if touch "${TESTFILE}" 2>/dev/null; then
        rm -f "${TESTFILE}"
    else
        ALL_WRITABLE=1
        echo "         Not writable: ${SERVICE_ROOT}/${dir}"
    fi
done
check "All writable directories are writable" ${ALL_WRITABLE}

# V1-8: shake on PATH
command -v shake >/dev/null 2>&1; check "'shake' is on PATH" $?

# V1-9: shake --help exits 0
shake --help >/dev/null 2>&1; check "'shake --help' exits 0" $?

# V1-10: Scripts exist inside image
SCRIPTS_OK=0
for script in configure-shakemap.sh build-shakemap-docker.sh verify-shakemap-build.sh verify-shakemap-config.sh; do
    if [ ! -f "/app/scripts/${script}" ]; then
        SCRIPTS_OK=1
        echo "         Missing: /app/scripts/${script}"
    fi
done
check "Scripts shipped inside image" ${SCRIPTS_OK}

# V1-11 to V1-14: HTTP checks via Python (curl may not be in slim image)
PYRESULT="$(python3 - "${PORT}" <<'PYEOF2'
import sys, json, urllib.request, urllib.error

port = sys.argv[1]

# /healthz
try:
    req = urllib.request.Request(f"http://localhost:{port}/healthz")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode()
        data = json.loads(body)
        healthz_ok = "true"
        stage1_passed = str(data.get("stage1", {}).get("passed", False)).lower()
        stage2_passed = str(data.get("stage2", {}).get("passed", False)).lower()
        status = data.get("status", "")
        has_blocking = "blocking_reasons" in data
        has_next = "next_action" in data
except Exception:
    healthz_ok = "false"
    stage1_passed = "false"
    stage2_passed = "true"
    status = ""
    has_blocking = False
    has_next = False

# POST /events/submit
submit_code = "0"
try:
    boundary = "----TestBoundary123"
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"event_id\"\r\n\r\ntest")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"user_id\"\r\n\r\ntest")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"test.txt\"\r\nContent-Type: text/plain\r\n\r\ntest")
    parts.append(f"--{boundary}--\r\n")
    body_bytes = "\r\n".join(parts).encode()
    req = urllib.request.Request(
        f"http://localhost:{port}/events/submit",
        data=body_bytes,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        submit_code = str(resp.status)
except urllib.error.HTTPError as e:
    submit_code = str(e.code)
except Exception:
    submit_code = "0"

print(f"{healthz_ok}|{stage1_passed}|{stage2_passed}|{status}|{submit_code}|{has_blocking}|{has_next}")
PYEOF2
)"

IFS='|' read -r HEALTHZ_OK STAGE1_PASSED STAGE2_PASSED STATUS_VAL SUBMIT_CODE HAS_BLOCKING HAS_NEXT <<< "${PYRESULT}"

[ "${HEALTHZ_OK}" = "true" ]; check "/healthz responds with JSON" $?
[ "${STAGE1_PASSED}" = "true" ]; check "/healthz stage1.passed == true" $?
[ "${STAGE2_PASSED}" = "false" ]; check "/healthz stage2.passed == false (not yet run)" $?
[ "${STATUS_VAL}" = "not_ready" ]; check "/healthz status == 'not_ready'" $?
[ "${SUBMIT_CODE}" = "503" ]; check "POST /events/submit returns HTTP 503 before Stage 2" $?

# -- Summary --
echo ""
echo "===== Stage 1 Results: ${PASS} passed, ${FAIL} failed (of ${TOTAL}) ====="

if [ ${FAIL} -gt 0 ]; then
    exit 1
else
    exit 0
fi
