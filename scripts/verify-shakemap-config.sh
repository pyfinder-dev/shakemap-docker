#!/usr/bin/env bash
set -uo pipefail

# ------------------------------------------------------------------
# verify-shakemap-config.sh -- Configuration / Readiness Verification
#
# Validates all Stage 2 guarantees (plan S12.2, V2-1 through V2-15).
# Run inside the container after configure-shakemap.sh.
#
# Usage:
#   docker exec <container> /app/scripts/verify-shakemap-config.sh
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
        echo "  PASS  V2-${TOTAL}: ${label}"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  V2-${TOTAL}: ${label}"
        FAIL=$((FAIL + 1))
    fi
}

RUNTIME_ROOT="${RUNTIME_ROOT:-/home/sysop/runtime}"
SERVICE_ROOT="${SERVICE_ROOT:-${RUNTIME_ROOT}/shakemap}"
PROFILE="${SHAKEMAP_PROFILE:-default}"
PORT="${SHAKEMAP_PORT:-9010}"
ALLOW_UNIFORM_VS30="${SHAKEMAP_ALLOW_UNIFORM_VS30:-0}"

HOME_DIR="${HOME:-/home/sysop}"
PROFILE_ROOT="${HOME_DIR}/shakemap_profiles/${PROFILE}"
PROFILE_DATA_DIR="${PROFILE_ROOT}/data"
PROFILE_INSTALL_DIR="${PROFILE_ROOT}/install"
PROFILE_CONFIG_DIR="${PROFILE_INSTALL_DIR}/config"
PROFILES_CONF="${HOME_DIR}/.shakemap/profiles.conf"
SENTINEL="${HOME_DIR}/.shakemap/.shakemap_readiness_status"

echo "===== Stage 2 Verification ====="
echo ""

# V2-1: Profile directory exists
[ -d "${PROFILE_ROOT}" ]; check "Profile directory exists: ${PROFILE_ROOT}" $?

# V2-2: profiles.conf lists active profile
PROFCONF_OK=1
if [ -f "${PROFILES_CONF}" ]; then
    if grep -q "${PROFILE}" "${PROFILES_CONF}" 2>/dev/null; then
        PROFCONF_OK=0
    fi
fi
check "profiles.conf lists profile '${PROFILE}'" ${PROFCONF_OK}

# V2-3: Data symlink resolves to SERVICE_ROOT/work
SYMLINK_OK=1
if [ -L "${PROFILE_DATA_DIR}" ]; then
    LINK_TARGET="$(readlink -f "${PROFILE_DATA_DIR}" 2>/dev/null || readlink "${PROFILE_DATA_DIR}")"
    EXPECTED="$(readlink -f "${SERVICE_ROOT}/work" 2>/dev/null || echo "${SERVICE_ROOT}/work")"
    if [ "${LINK_TARGET}" = "${EXPECTED}" ]; then
        SYMLINK_OK=0
    else
        echo "         Symlink target: ${LINK_TARGET} (expected: ${EXPECTED})"
    fi
fi
check "Data symlink resolves to SERVICE_ROOT/work" ${SYMLINK_OK}

# V2-4: model.conf vs30file resolves
MODEL_CONF="${PROFILE_CONFIG_DIR}/model.conf"
VS30_VALID=1
VS30_PATH=""
if [ -f "${MODEL_CONF}" ]; then
    VS30_LINE="$(grep -E '^[[:space:]]*vs30file[[:space:]]*=' "${MODEL_CONF}" 2>/dev/null || echo "")"
    VS30_PATH="$(echo "${VS30_LINE}" | sed 's/^[[:space:]]*vs30file[[:space:]]*=[[:space:]]*//' | sed 's/[[:space:]]*$//')"
    if [ -n "${VS30_PATH}" ] && [ -f "${VS30_PATH}" ]; then
        VS30_VALID=0
    elif [ -z "${VS30_PATH}" ] && [ "${ALLOW_UNIFORM_VS30}" = "1" ]; then
        VS30_VALID=0
    fi
fi
check "model.conf vs30file resolves (or uniform allowed)" ${VS30_VALID}

# V2-5: No CA_vs30.grd reference in model.conf
NO_CA=1
if [ -f "${MODEL_CONF}" ]; then
    if ! grep -q "CA_vs30.grd" "${MODEL_CONF}" 2>/dev/null; then
        NO_CA=0
    fi
fi
check "No CA_vs30.grd reference in model.conf" ${NO_CA}

# V2-6: VS30 grid file exists (unless uniform allowed)
VS30_EXISTS=1
if [ "${ALLOW_UNIFORM_VS30}" = "1" ] && [ -z "${VS30_PATH:-}" ]; then
    VS30_EXISTS=0
elif [ -n "${VS30_PATH:-}" ] && [ -f "${VS30_PATH}" ]; then
    VS30_EXISTS=0
fi
check "VS30 grid file exists (or uniform allowed)" ${VS30_EXISTS}

# V2-7: VS30 grid file non-empty (unless uniform allowed)
VS30_NONEMPTY=1
if [ "${ALLOW_UNIFORM_VS30}" = "1" ] && [ -z "${VS30_PATH:-}" ]; then
    VS30_NONEMPTY=0
elif [ -n "${VS30_PATH:-}" ] && [ -s "${VS30_PATH}" ]; then
    VS30_NONEMPTY=0
fi
check "VS30 grid file non-empty (or uniform allowed)" ${VS30_NONEMPTY}

# V2-8: Topo grid exists or disabled
TOPO_OK=1
PRODUCTS_CONF="${PROFILE_CONFIG_DIR}/products.conf"
if [ -f "${PRODUCTS_CONF}" ]; then
    TOPO_LINE="$(grep -E '^[[:space:]]*topography[[:space:]]*=' "${PRODUCTS_CONF}" 2>/dev/null || echo "")"
    TOPO_PATH="$(echo "${TOPO_LINE}" | sed 's/^[[:space:]]*topography[[:space:]]*=[[:space:]]*//' | sed 's/[[:space:]]*$//')"
    if [ -n "${TOPO_PATH}" ] && [ -f "${TOPO_PATH}" ]; then
        TOPO_OK=0
    elif [ -z "${TOPO_PATH}" ]; then
        # Topo disabled -- that's OK
        TOPO_OK=0
    fi
else
    # No products.conf -- topo implicitly disabled
    TOPO_OK=0
fi
check "Topo grid exists or explicitly disabled" ${TOPO_OK}

# V2-9: Stage 2 sentinel file exists and says "ready"
SENTINEL_OK=1
if [ -f "${SENTINEL}" ]; then
    SENTINEL_CONTENT="$(cat "${SENTINEL}")"
    if echo "${SENTINEL_CONTENT}" | grep -q "^ready"; then
        SENTINEL_OK=0
    else
        echo "         Sentinel content: ${SENTINEL_CONTENT}"
    fi
fi
check "Stage 2 sentinel says 'ready'" ${SENTINEL_OK}

# V2-10 to V2-12: HTTP checks via Python (curl may not be in slim image)
PYRESULT="$(python3 - "${PORT}" <<'PYEOF'
import sys, json, urllib.request, urllib.error

port = sys.argv[1]

# /healthz
stage2_passed = "false"
status = ""
try:
    req = urllib.request.Request(f"http://localhost:{port}/healthz")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
        stage2_passed = str(data.get("stage2", {}).get("passed", False)).lower()
        status = data.get("status", "")
except Exception:
    pass

# POST /events/submit -- expect NOT 503 (gate should be open)
submit_code = "0"
try:
    boundary = "----TestBoundary123"
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"event_id\"\r\n\r\ntest_gate")
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
    pass

print(f"{stage2_passed}|{status}|{submit_code}")
PYEOF
)"

IFS='|' read -r S2PASSED STATUS_VAL SUBMIT_CODE <<< "${PYRESULT}"

[ "${S2PASSED}" = "true" ]; check "/healthz stage2.passed == true" ${S2PASSED:+$?}
[ "${STATUS_VAL}" = "healthy" ]; check "/healthz status == 'healthy'" $?

# Submit gate should be open (not 503)
GATE_OK=1
if [ "${SUBMIT_CODE}" != "503" ] && [ "${SUBMIT_CODE}" != "0" ]; then
    GATE_OK=0
fi
check "POST /events/submit no longer returns 503 (gate open)" ${GATE_OK}

# -- Idempotency check --
echo ""
echo "--- Idempotency check: re-running configure-shakemap.sh ---"
if /app/scripts/configure-shakemap.sh >/dev/null 2>&1; then
    check "configure-shakemap.sh idempotent re-run exits 0" 0
else
    check "configure-shakemap.sh idempotent re-run exits 0" 1
fi

# After re-run: sentinel still ready
IDEM_SENTINEL=1
if [ -f "${SENTINEL}" ]; then
    if grep -q "^ready" "${SENTINEL}" 2>/dev/null; then
        IDEM_SENTINEL=0
    fi
fi
check "Sentinel still 'ready' after re-run" ${IDEM_SENTINEL}

# After re-run: healthz still healthy
IDEM_HEALTH=1
IDEM_STATUS="$(python3 - "${PORT}" <<'PYEOF2'
import sys, json, urllib.request
port = sys.argv[1]
try:
    req = urllib.request.Request(f"http://localhost:{port}/healthz")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
        print(data.get("status", ""))
except Exception:
    print("")
PYEOF2
)"
[ "${IDEM_STATUS}" = "healthy" ] && IDEM_HEALTH=0
check "/healthz still 'healthy' after idempotent re-run" ${IDEM_HEALTH}

# -- Summary --
echo ""
echo "===== Stage 2 Results: ${PASS} passed, ${FAIL} failed (of ${TOTAL}) ====="

if [ ${FAIL} -gt 0 ]; then
    exit 1
else
    exit 0
fi
