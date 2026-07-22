#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# run-shakemap-ci-tests.sh -- Full Two-Stage CI Test
#
# Builds the Docker image, starts a container, runs build and config
# verification, and reports results.
#
# Scripts are shipped inside the image -- no docker cp required.
#
# Usage:
#   ./scripts/run-shakemap-ci-tests.sh
#
# Exit code: 0 if all checks pass, 1 if any fail.
# ------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_TAG="shakemap-docker:integration-test"
CONTAINER_NAME="shakemap-docker-ci-$$"
QA_RUNTIME="$(mktemp -d /private/tmp/shakemap-docker-ci.XXXXXX)"
CLEANUP_DONE=0
OVERALL_RESULT=0

cleanup() {
    if [ "${CLEANUP_DONE}" = "0" ]; then
        CLEANUP_DONE=1
        echo ""
        echo "--- Cleanup ---"
        docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
        rm -rf -- "${QA_RUNTIME}"
    fi
}
trap cleanup EXIT

log() { echo "[integration-test] $*"; }

echo "===== Two-Stage Integration Test ====="
echo ""

# -- [1/11] Build Docker image --
log "[1/11] Building Docker image"
"${SCRIPT_DIR}/build-shakemap-docker.sh" --tag "${IMAGE_TAG}"
echo ""

# -- [2/11] Verify scripts exist inside image --
log "[2/11] Verifying scripts inside image"
SCRIPTS_CHECK="$(docker run --rm "${IMAGE_TAG}" ls -1 /app/scripts/ 2>/dev/null)"
SCRIPTS_OK=0
for script in configure-shakemap.sh build-shakemap-docker.sh verify-shakemap-build.sh verify-shakemap-config.sh inspect-shakemap-config.sh; do
    if ! echo "${SCRIPTS_CHECK}" | grep -q "${script}"; then
        echo "  FAIL: /app/scripts/${script} not found in image"
        SCRIPTS_OK=1
    fi
done
if [ "${SCRIPTS_OK}" = "0" ]; then
    echo "  All required scripts found inside image"
else
    echo "  ERROR: Some scripts missing from image"
    OVERALL_RESULT=1
fi
echo ""

# -- [3/11] Start container --
log "[3/11] Starting container"
docker run -d --name "${CONTAINER_NAME}" \
    -v "${QA_RUNTIME}:/home/sysop/runtime" \
    -e SHAKEMAP_SKIP_DATA_DOWNLOAD=1 \
    -e SHAKEMAP_ALLOW_UNIFORM_VS30=1 \
    "${IMAGE_TAG}"

# Wait for service to start
echo "  Waiting for service to start..."
sleep 5
echo ""

# -- [4/11] Stage 1 verification --
log "[4/11] Running build verification"
if docker exec "${CONTAINER_NAME}" /app/scripts/verify-shakemap-build.sh; then
    echo ""
    echo "  Stage 1: ALL CHECKS PASSED"
    STAGE1_RESULT="PASS"
else
    echo ""
    echo "  Stage 1: SOME CHECKS FAILED"
    STAGE1_RESULT="FAIL"
    OVERALL_RESULT=1
fi
echo ""

# -- [5/11] Verify /healthz returns detailed diagnostics --
log "[5/11] Verifying /healthz detailed diagnostics"
HEALTHZ_CHECK="$(docker exec "${CONTAINER_NAME}" python3 - <<'PYEOF'
import json, urllib.request
try:
    req = urllib.request.Request("http://localhost:9010/healthz")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
        checks = []
        checks.append(("blocking_reasons present", "blocking_reasons" in data))
        checks.append(("next_action present", "next_action" in data))
        checks.append(("stage1.checks present", "checks" in data.get("stage1", {})))
        checks.append(("stage2.checks present", "checks" in data.get("stage2", {})))
        checks.append(("status is not_ready", data.get("status") == "not_ready"))
        checks.append(("blocking_reasons is list", isinstance(data.get("blocking_reasons"), list)))
        checks.append(("blocking_reasons non-empty", len(data.get("blocking_reasons", [])) > 0))
        all_ok = True
        for label, ok in checks:
            status = "PASS" if ok else "FAIL"
            print(f"  {status}: {label}")
            if not ok:
                all_ok = False
        if all_ok:
            print("RESULT:PASS")
        else:
            print("RESULT:FAIL")
except Exception as e:
    print(f"  FAIL: /healthz request failed: {e}")
    print("RESULT:FAIL")
PYEOF
)"
echo "${HEALTHZ_CHECK}"
if echo "${HEALTHZ_CHECK}" | grep -q "RESULT:PASS"; then
    echo "  Diagnostics: PASS"
else
    echo "  Diagnostics: FAIL"
    OVERALL_RESULT=1
fi
echo ""

# -- [6/11] Verify /events/submit returns 503 before Stage 2 --
log "[6/11] Verifying submit gate (should be 503)"
SUBMIT_PRE="$(docker exec "${CONTAINER_NAME}" python3 - <<'PYEOF'
import urllib.request, urllib.error
try:
    boundary = "----TestBoundary123"
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"event_id\"\r\n\r\ntest")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"user_id\"\r\n\r\ntest")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"test.txt\"\r\nContent-Type: text/plain\r\n\r\ntest")
    parts.append(f"--{boundary}--\r\n")
    body_bytes = "\r\n".join(parts).encode()
    req = urllib.request.Request(
        "http://localhost:9010/events/submit",
        data=body_bytes,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print(f"ERROR:{e}")
PYEOF
)"
if [ "${SUBMIT_PRE}" = "503" ]; then
    echo "  PASS: /events/submit returns HTTP 503 before Stage 2"
else
    echo "  FAIL: /events/submit returned ${SUBMIT_PRE} (expected 503)"
    OVERALL_RESULT=1
fi
echo ""

# -- [7/11] Run configure-shakemap.sh --
log "[7/11] Running configure-shakemap.sh (Stage 2)"
if docker exec "${CONTAINER_NAME}" /app/scripts/configure-shakemap.sh; then
    echo "  Stage 2 configure: PASS"
else
    echo "  Stage 2 configure: FAIL"
    OVERALL_RESULT=1
fi
echo ""

# -- [8/11] Verify /events/submit no longer returns 503 --
log "[8/11] Verifying submit gate is open (should NOT be 503)"
SUBMIT_POST="$(docker exec "${CONTAINER_NAME}" python3 - <<'PYEOF'
import urllib.request, urllib.error
try:
    boundary = "----TestBoundary123"
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"event_id\"\r\n\r\ntest_after")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"user_id\"\r\n\r\ntest")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"test.txt\"\r\nContent-Type: text/plain\r\n\r\ntest")
    parts.append(f"--{boundary}--\r\n")
    body_bytes = "\r\n".join(parts).encode()
    req = urllib.request.Request(
        "http://localhost:9010/events/submit",
        data=body_bytes,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print(f"ERROR:{e}")
PYEOF
)"
if [ "${SUBMIT_POST}" != "503" ] && [ "${SUBMIT_POST}" != "0" ]; then
    echo "  PASS: /events/submit returned HTTP ${SUBMIT_POST} (not 503)"
else
    echo "  FAIL: /events/submit still returns ${SUBMIT_POST}"
    OVERALL_RESULT=1
fi
echo ""

# -- [9/11] Stage 2 verification (includes idempotency) --
log "[9/11] Running config verification (includes idempotency check)"
if docker exec "${CONTAINER_NAME}" /app/scripts/verify-shakemap-config.sh; then
    echo ""
    echo "  Stage 2: ALL CHECKS PASSED"
    STAGE2_RESULT="PASS"
else
    echo ""
    echo "  Stage 2: SOME CHECKS FAILED"
    STAGE2_RESULT="FAIL"
    OVERALL_RESULT=1
fi
echo ""

# -- [10/11] Verify /config endpoint --
log "[10/11] Verifying /config endpoint"
CONFIG_CHECK="$(docker exec "${CONTAINER_NAME}" python3 - <<'PYEOF'
import json, urllib.request
try:
    with urllib.request.urlopen("http://localhost:9010/config", timeout=10) as r:
        data = json.loads(r.read().decode())
        checks = []
        checks.append(("active_profile present", "active_profile" in data))
        checks.append(("readiness_state present", "readiness_state" in data))
        checks.append(("overrides present", "overrides" in data))
        checks.append(("override_warnings present", "override_warnings" in data))
        checks.append(("shakemap_modules present", "shakemap_modules" in data))
        checks.append(("vs30_file present", "vs30_file" in data))
        all_ok = True
        for label, ok in checks:
            status = "PASS" if ok else "FAIL"
            print(f"  {status}: {label}")
            if not ok:
                all_ok = False
        if all_ok:
            print("RESULT:PASS")
        else:
            print("RESULT:FAIL")
except Exception as e:
    print(f"  FAIL: /config request failed: {e}")
    print("RESULT:FAIL")
PYEOF
)"
echo "${CONFIG_CHECK}"
if echo "${CONFIG_CHECK}" | grep -q "RESULT:PASS"; then
    echo "  /config endpoint: PASS"
else
    echo "  /config endpoint: FAIL"
    OVERALL_RESULT=1
fi
echo ""

# -- [11/11] Verify inspect-shakemap-config.sh --
log "[11/11] Running inspect-shakemap-config.sh"
INSPECT_OUTPUT="$(docker exec "${CONTAINER_NAME}" /app/scripts/inspect-shakemap-config.sh 2>&1)"
INSPECT_RC=$?
if [ "${INSPECT_RC}" = "0" ] && echo "${INSPECT_OUTPUT}" | grep -q "Active Profile:"; then
    echo "  inspect-shakemap-config.sh: PASS"
else
    echo "  inspect-shakemap-config.sh: FAIL (rc=${INSPECT_RC})"
    echo "${INSPECT_OUTPUT}" | head -20
    OVERALL_RESULT=1
fi
echo ""

# -- Summary --
echo "===== Integration Test Summary ====="
echo "  Stage 1 verification:     ${STAGE1_RESULT}"
echo "  Stage 2 verification:     ${STAGE2_RESULT}"
echo "  Scripts in image:         $([ "${SCRIPTS_OK}" = "0" ] && echo PASS || echo FAIL)"
echo "  Diagnostics:              $(echo "${HEALTHZ_CHECK}" | grep -q "RESULT:PASS" && echo PASS || echo FAIL)"
echo "  Submit gate pre-Stage2:   $([ "${SUBMIT_PRE}" = "503" ] && echo PASS || echo FAIL)"
echo "  Submit gate post-Stage2:  $([ "${SUBMIT_POST}" != "503" ] && echo PASS || echo FAIL)"
echo "  /config endpoint:         $(echo "${CONFIG_CHECK}" | grep -q "RESULT:PASS" && echo PASS || echo FAIL)"
echo "  inspect-shakemap-config:  $([ "${INSPECT_RC}" = "0" ] && echo PASS || echo FAIL)"
echo "====================================="

if [ "${OVERALL_RESULT}" != "0" ]; then
    echo ""
    echo "INTEGRATION TEST FAILED"
    exit 1
fi

echo ""
echo "ALL INTEGRATION TESTS PASSED"
exit 0
