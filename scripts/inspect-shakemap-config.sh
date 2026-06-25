#!/usr/bin/env bash
set -uo pipefail

# ------------------------------------------------------------------
# inspect-shakemap-config.sh -- ShakeMap Configuration Inspector
#
# Prints the active ShakeMap configuration from within the running
# container.  Two modes:
#
#   (default)    Read configuration directly from filesystem
#   --rest       Query the REST API /config endpoint instead
#
# Usage:
#   docker exec <container> /app/scripts/inspect-shakemap-config.sh
#   docker exec <container> /app/scripts/inspect-shakemap-config.sh --rest
#
# Exit code: always 0 (inspection only, never fails).
# ------------------------------------------------------------------

MODE="filesystem"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rest)
            MODE="rest"; shift
            ;;
        --help|-h)
            echo "Usage: $0 [--rest]"
            echo ""
            echo "  (default)  Read config from filesystem"
            echo "  --rest     Query the /config REST endpoint"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option: $1" >&2
            echo "Run with --help for usage." >&2
            exit 1
            ;;
    esac
done

# ------------------------------------------------------------------
# REST mode: query /config endpoint
# ------------------------------------------------------------------
if [ "${MODE}" = "rest" ]; then
    PORT="${SHAKEMAP_PORT:-9010}"
    python3 - "${PORT}" <<'PYEOF'
import sys, json, urllib.request

port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://localhost:{port}/config", timeout=10) as resp:
        data = json.loads(resp.read().decode())
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f"ERROR: Could not reach /config endpoint: {e}", file=sys.stderr)
    # Fall through -- exit 0 regardless
PYEOF
    exit 0
fi

# ------------------------------------------------------------------
# Filesystem mode: read config directly
# ------------------------------------------------------------------

RUNTIME_ROOT="${RUNTIME_ROOT:-/home/sysop/runtime}"
SERVICE_ROOT="${SERVICE_ROOT:-${RUNTIME_ROOT}/shakemap}"
PROFILE="${SHAKEMAP_PROFILE:-default}"
ALLOW_UNIFORM_VS30="${SHAKEMAP_ALLOW_UNIFORM_VS30:-0}"

HOME_DIR="${HOME:-/home/sysop}"
PROFILE_ROOT="${HOME_DIR}/shakemap_profiles/${PROFILE}"
PROFILE_INSTALL_DIR="${PROFILE_ROOT}/install"
PROFILE_CONFIG_DIR="${PROFILE_INSTALL_DIR}/config"
PROFILE_DATA_DIR="${PROFILE_ROOT}/data"
PROFILES_CONF="${HOME_DIR}/.shakemap/profiles.conf"
SENTINEL="${HOME_DIR}/.shakemap/.shakemap_readiness_status"

SHAKEMAP_DATA_DIR="${SERVICE_ROOT}/data"
VS30_DEFAULT="${SHAKEMAP_DATA_DIR}/vs30/global_vs30.grd"
TOPO_DEFAULT="${SHAKEMAP_DATA_DIR}/topo/topo_30sec.grd"

echo "===== ShakeMap Configuration ====="
echo ""

# -- Active profile --
echo "Active Profile:        ${PROFILE}"

# -- Available profiles --
AVAILABLE=""
if [ -d "${HOME_DIR}/shakemap_profiles" ]; then
    AVAILABLE="$(ls -1 "${HOME_DIR}/shakemap_profiles" 2>/dev/null | tr '\n' ', ' | sed 's/,$//')"
fi
echo "Available Profiles:    ${AVAILABLE:-none}"

# -- Paths --
echo ""
echo "--- Paths ---"
echo "Profiles.conf:         ${PROFILES_CONF}"
printf "  exists:              "
[ -f "${PROFILES_CONF}" ] && echo "yes" || echo "NO"

echo "Model.conf:            ${PROFILE_CONFIG_DIR}/model.conf"
printf "  exists:              "
[ -f "${PROFILE_CONFIG_DIR}/model.conf" ] && echo "yes" || echo "NO"

echo "Products.conf:         ${PROFILE_CONFIG_DIR}/products.conf"
printf "  exists:              "
[ -f "${PROFILE_CONFIG_DIR}/products.conf" ] && echo "yes" || echo "NO"

echo "Profile root:          ${PROFILE_ROOT}"
printf "  exists:              "
[ -d "${PROFILE_ROOT}" ] && echo "yes" || echo "NO"

echo "Profile config dir:    ${PROFILE_CONFIG_DIR}"
printf "  exists:              "
[ -d "${PROFILE_CONFIG_DIR}" ] && echo "yes" || echo "NO"

echo "Profile data dir:      ${PROFILE_DATA_DIR}"
if [ -L "${PROFILE_DATA_DIR}" ]; then
    TARGET="$(readlink -f "${PROFILE_DATA_DIR}" 2>/dev/null || readlink "${PROFILE_DATA_DIR}")"
    echo "  symlink -> ${TARGET}"
elif [ -d "${PROFILE_DATA_DIR}" ]; then
    echo "  directory (NOT a symlink)"
else
    echo "  MISSING"
fi

# -- VS30 --
echo ""
echo "--- VS30 Grid ---"
VS30_FILE=""
CUSTOM_VS30="${SHAKEMAP_VS30_FILE:-}"
if [ -n "${CUSTOM_VS30}" ] && [ -f "${CUSTOM_VS30}" ]; then
    VS30_FILE="${CUSTOM_VS30}"
    echo "VS30 source:           custom (SHAKEMAP_VS30_FILE)"
elif [ -f "${VS30_DEFAULT}" ]; then
    VS30_FILE="${VS30_DEFAULT}"
    echo "VS30 source:           default"
else
    for f in "${SHAKEMAP_DATA_DIR}"/vs30/*.grd; do
        if [ -f "$f" ]; then
            VS30_FILE="$f"
            echo "VS30 source:           regional"
            break
        fi
    done
fi

if [ -n "${VS30_FILE}" ]; then
    VS30_SIZE="$(stat -c '%s' "${VS30_FILE}" 2>/dev/null || stat -f '%z' "${VS30_FILE}" 2>/dev/null || echo "?")"
    echo "VS30 file:             ${VS30_FILE}"
    echo "VS30 size:             ${VS30_SIZE} bytes"
else
    echo "VS30 file:             NOT FOUND"
    if [ "${ALLOW_UNIFORM_VS30}" = "1" ]; then
        echo "VS30 override:         UNIFORM VS30 (760 m/s) -- DEVELOPMENT/EMERGENCY OVERRIDE ACTIVE"
    else
        echo "VS30 override:         not allowed (SHAKEMAP_ALLOW_UNIFORM_VS30=0)"
    fi
fi

# -- model.conf vs30file --
MODEL_CONF="${PROFILE_CONFIG_DIR}/model.conf"
if [ -f "${MODEL_CONF}" ]; then
    VS30_LINE="$(grep -E '^[[:space:]]*vs30file[[:space:]]*=' "${MODEL_CONF}" 2>/dev/null || echo "")"
    VS30_PATH="$(echo "${VS30_LINE}" | sed 's/^[[:space:]]*vs30file[[:space:]]*=[[:space:]]*//' | sed 's/[[:space:]]*$//')"
    echo "model.conf vs30file:   ${VS30_PATH:-<empty>}"
    if [ -n "${VS30_PATH}" ]; then
        printf "  resolves:            "
        [ -f "${VS30_PATH}" ] && echo "yes" || echo "NO"
    fi
fi

# -- Topo --
echo ""
echo "--- Topography Grid ---"
TOPO_FILE=""
CUSTOM_TOPO="${SHAKEMAP_TOPO_FILE:-}"
if [ -n "${CUSTOM_TOPO}" ] && [ -f "${CUSTOM_TOPO}" ]; then
    TOPO_FILE="${CUSTOM_TOPO}"
    echo "Topo source:           custom (SHAKEMAP_TOPO_FILE)"
elif [ -f "${TOPO_DEFAULT}" ]; then
    TOPO_FILE="${TOPO_DEFAULT}"
    echo "Topo source:           default"
fi

if [ -n "${TOPO_FILE}" ]; then
    TOPO_SIZE="$(stat -c '%s' "${TOPO_FILE}" 2>/dev/null || stat -f '%z' "${TOPO_FILE}" 2>/dev/null || echo "?")"
    echo "Topo file:             ${TOPO_FILE}"
    echo "Topo size:             ${TOPO_SIZE} bytes"
else
    echo "Topo file:             NOT FOUND (topography disabled)"
fi

# -- Readiness --
echo ""
echo "--- Readiness ---"
if [ -f "${SENTINEL}" ]; then
    SENTINEL_CONTENT="$(cat "${SENTINEL}")"
    if echo "${SENTINEL_CONTENT}" | grep -q "^ready"; then
        # Check for overrides
        if echo "${SENTINEL_CONTENT}" | grep -q "uniform_vs30_override"; then
            echo "Readiness state:       READY (with uniform VS30 override)"
            echo "  WARNING: This is a development/emergency override."
            echo "  Production deployments should provide a VS30 grid."
        else
            echo "Readiness state:       READY"
        fi
    else
        REASON="$(echo "${SENTINEL_CONTENT}" | cut -d'|' -f2-)"
        echo "Readiness state:       NOT READY"
        echo "  Reason: ${REASON}"
    fi
else
    echo "Readiness state:       NOT CONFIGURED"
    echo "  Run: docker exec <container> /app/scripts/configure-shakemap.sh"
fi

# -- Modules --
echo ""
echo "--- ShakeMap Modules ---"
echo "Modules:               ${SHAKEMAP_MODULES:-select assemble model contour mapping stations gridxml}"

# -- Service --
echo ""
echo "--- Service ---"
echo "Service root:          ${SERVICE_ROOT}"
echo "Runtime root:          ${RUNTIME_ROOT}"

echo ""
echo "===== End Configuration ====="
