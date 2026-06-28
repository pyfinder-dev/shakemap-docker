#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# Stage 2 -- ShakeMap Configuration / Readiness
#
# This script configures the active ShakeMap profile, provisions
# required data files, and patches configuration to point to real
# data paths.  It is invoked explicitly by the operator after the
# container is running (Stage 1 complete).
#
# Idempotent: safe to run multiple times.
#
# Usage:
#   docker exec <container> /app/scripts/configure-shakemap.sh
#
# Environment variables:
#   SHAKEMAP_PROFILE              Profile name (default: "default")
#   SHAKEMAP_SKIP_DATA_DOWNLOAD   Set to "1" to skip USGS downloads
#   SHAKEMAP_ALLOW_UNIFORM_VS30   Set to "1" to allow empty vs30file
#   SHAKEMAP_VS30_FILE            Custom path to VS30 grid file
#   SHAKEMAP_TOPO_FILE            Custom path to topography grid file
#   SERVICE_ROOT                  ShakeMap service root
#   RUNTIME_ROOT                  Top-level runtime root
# ------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -- Environment --
RUNTIME_ROOT="${RUNTIME_ROOT:-/home/sysop/runtime}"
SERVICE_ROOT="${SERVICE_ROOT:-${RUNTIME_ROOT}/shakemap}"
PROFILE="${SHAKEMAP_PROFILE:-default}"
SKIP_DOWNLOAD="${SHAKEMAP_SKIP_DATA_DOWNLOAD:-0}"
ALLOW_UNIFORM_VS30="${SHAKEMAP_ALLOW_UNIFORM_VS30:-0}"
CUSTOM_VS30="${SHAKEMAP_VS30_FILE:-}"
CUSTOM_TOPO="${SHAKEMAP_TOPO_FILE:-}"

HOME_DIR="${HOME:-/home/sysop}"
PROFILE_ROOT="${HOME_DIR}/shakemap_profiles/${PROFILE}"
PROFILE_DATA_DIR="${PROFILE_ROOT}/data"
PROFILE_INSTALL_DIR="${PROFILE_ROOT}/install"
PROFILE_CONFIG_DIR="${PROFILE_INSTALL_DIR}/config"
PROFILES_CONF="${HOME_DIR}/.shakemap/profiles.conf"
SENTINEL="${HOME_DIR}/.shakemap/.shakemap_readiness_status"

SHAKEMAP_DATA_DIR="${SERVICE_ROOT}/data"
VS30_DEFAULT="${SHAKEMAP_DATA_DIR}/vs30/global_vs30.grd"
TOPO_DEFAULT="${SHAKEMAP_DATA_DIR}/topo/topo_30sec.grd"

VS30_URL="https://apps.usgs.gov/shakemap_geodata/vs30/global_vs30.grd"
TOPO_URL="https://apps.usgs.gov/shakemap_geodata/topo/topo_30sec.grd"

ERRORS=""

log()   { echo "[configure-shakemap] $*"; }
warn()  { echo "[configure-shakemap] WARNING: $*" >&2; }
fail()  { ERRORS="${ERRORS}${ERRORS:+; }$*"; warn "$*"; }

# -- [1/9] Create profile --
log "[1/9] Checking ShakeMap profile"
log "  Profile: ${PROFILE}"
log "  Profile root: ${PROFILE_ROOT}"

if [ ! -d "${PROFILE_ROOT}" ] || [ ! -f "${PROFILES_CONF}" ]; then
    log "  Creating ShakeMap profile '${PROFILE}' via sm_profile..."
    # -c PROFILE : create and switch
    # -a         : accept defaults
    # -n         : skip topo & Vs30 grid downloads (handled below)
    sm_profile -c "${PROFILE}" -a -n
else
    log "  Profile '${PROFILE}' already exists."
fi

# -- [2/9] Ensure profile data -> SERVICE_ROOT/work symlink --
log "[2/9] Setting up data symlink"
if [ -L "${PROFILE_DATA_DIR}" ]; then
    CURRENT_TARGET="$(readlink -f "${PROFILE_DATA_DIR}" 2>/dev/null || readlink "${PROFILE_DATA_DIR}")"
    EXPECTED_TARGET="$(readlink -f "${SERVICE_ROOT}/.service/work" 2>/dev/null || echo "${SERVICE_ROOT}/.service/work")"
    if [ "${CURRENT_TARGET}" = "${EXPECTED_TARGET}" ]; then
        log "  Data symlink already correct: ${PROFILE_DATA_DIR} -> ${SERVICE_ROOT}/.service/work"
    else
        log "  Updating data symlink: ${PROFILE_DATA_DIR} -> ${SERVICE_ROOT}/.service/work"
        rm -f "${PROFILE_DATA_DIR}"
        ln -s "${SERVICE_ROOT}/.service/work" "${PROFILE_DATA_DIR}"
    fi
elif [ -e "${PROFILE_DATA_DIR}" ]; then
    log "  Removing existing data dir to replace with symlink."
    rmdir "${PROFILE_DATA_DIR}" 2>/dev/null || rm -rf "${PROFILE_DATA_DIR}"
    ln -s "${SERVICE_ROOT}/.service/work" "${PROFILE_DATA_DIR}"
    log "  Linked ${PROFILE_DATA_DIR} -> ${SERVICE_ROOT}/.service/work"
else
    ln -s "${SERVICE_ROOT}/.service/work" "${PROFILE_DATA_DIR}"
    log "  Linked ${PROFILE_DATA_DIR} -> ${SERVICE_ROOT}/.service/work"
fi

# -- [3/9] Run shake init if needed --
log "[3/9] Checking shake init"
# Sentinel: install/config/model.conf (NOT install/config/ alone,
# because sm_profile -c creates install/config/ as a side effect).
if [ ! -f "${PROFILE_CONFIG_DIR}/model.conf" ]; then
    log "  Running 'shake init' for profile '${PROFILE}'..."
    shake init
else
    log "  model.conf already exists; skipping 'shake init'."
fi

# -- [4/9] Detect / provision VS30 grid --
log "[4/9] Detecting VS30 grid"
VS30_FILE=""

# Priority 1: custom env var
if [ -n "${CUSTOM_VS30}" ] && [ -f "${CUSTOM_VS30}" ]; then
    VS30_FILE="${CUSTOM_VS30}"
    log "  VS30: using custom file from SHAKEMAP_VS30_FILE: ${VS30_FILE}"
fi

# Priority 2: default service data location
if [ -z "${VS30_FILE}" ] && [ -f "${VS30_DEFAULT}" ]; then
    VS30_FILE="${VS30_DEFAULT}"
    log "  VS30: found at default location: ${VS30_FILE}"
fi

# Priority 3: glob in service data vs30 dir
if [ -z "${VS30_FILE}" ]; then
    for f in "${SHAKEMAP_DATA_DIR}"/vs30/*.grd; do
        if [ -f "$f" ]; then
            VS30_FILE="$f"
            log "  VS30: found regional grid: ${VS30_FILE}"
            break
        fi
    done
fi

# Priority 4: glob in profile install data
if [ -z "${VS30_FILE}" ]; then
    for f in "${PROFILE_INSTALL_DIR}"/data/vs30/*.grd; do
        if [ -f "$f" ]; then
            VS30_FILE="$f"
            log "  VS30: found in profile install dir: ${VS30_FILE}"
            break
        fi
    done
fi

# Not found -- download or fail
if [ -z "${VS30_FILE}" ]; then
    if [ "${SKIP_DOWNLOAD}" = "1" ]; then
        if [ "${ALLOW_UNIFORM_VS30}" = "1" ]; then
            log "  VS30: no grid found, downloads disabled, uniform VS30 (760 m/s) allowed by operator."
            VS30_FILE=""
        else
            fail "VS30 grid not found and SHAKEMAP_SKIP_DATA_DOWNLOAD=1. Set SHAKEMAP_ALLOW_UNIFORM_VS30=1 to explicitly allow uniform VS30."
        fi
    else
        log "  VS30: downloading global_vs30.grd (~700 MB) to ${VS30_DEFAULT}..."
        mkdir -p "$(dirname "${VS30_DEFAULT}")"
        if curl -fSL --retry 3 --retry-delay 10 \
                -o "${VS30_DEFAULT}.tmp" \
                "${VS30_URL}"; then
            mv "${VS30_DEFAULT}.tmp" "${VS30_DEFAULT}"
            VS30_FILE="${VS30_DEFAULT}"
            log "  VS30: download complete."
        else
            rm -f "${VS30_DEFAULT}.tmp"
            fail "VS30 grid download failed from ${VS30_URL}"
        fi
    fi
fi

# -- [5/9] Detect / provision topography grid --
log "[5/9] Detecting topography grid"
TOPO_FILE=""

# Priority 1: custom env var
if [ -n "${CUSTOM_TOPO}" ] && [ -f "${CUSTOM_TOPO}" ]; then
    TOPO_FILE="${CUSTOM_TOPO}"
    log "  Topo: using custom file from SHAKEMAP_TOPO_FILE: ${TOPO_FILE}"
fi

# Priority 2: default service data location
if [ -z "${TOPO_FILE}" ] && [ -f "${TOPO_DEFAULT}" ]; then
    TOPO_FILE="${TOPO_DEFAULT}"
    log "  Topo: found at default location: ${TOPO_FILE}"
fi

# Priority 3: glob in service data topo dir
if [ -z "${TOPO_FILE}" ]; then
    for f in "${SHAKEMAP_DATA_DIR}"/topo/*.grd; do
        if [ -f "$f" ]; then
            TOPO_FILE="$f"
            log "  Topo: found grid: ${TOPO_FILE}"
            break
        fi
    done
fi

# Priority 4: glob in profile install data
if [ -z "${TOPO_FILE}" ]; then
    for f in "${PROFILE_INSTALL_DIR}"/data/topo/*.grd; do
        if [ -f "$f" ]; then
            TOPO_FILE="$f"
            log "  Topo: found in profile install dir: ${TOPO_FILE}"
            break
        fi
    done
fi

# Not found -- download or skip
if [ -z "${TOPO_FILE}" ]; then
    if [ "${SKIP_DOWNLOAD}" = "1" ]; then
        log "  Topo: no grid found, downloads disabled. Topography will be disabled."
    else
        log "  Topo: downloading topo_30sec.grd (~300 MB) to ${TOPO_DEFAULT}..."
        mkdir -p "$(dirname "${TOPO_DEFAULT}")"
        if curl -fSL --retry 3 --retry-delay 10 \
                -o "${TOPO_DEFAULT}.tmp" \
                "${TOPO_URL}"; then
            mv "${TOPO_DEFAULT}.tmp" "${TOPO_DEFAULT}"
            TOPO_FILE="${TOPO_DEFAULT}"
            log "  Topo: download complete."
        else
            rm -f "${TOPO_DEFAULT}.tmp"
            warn "Topo grid download failed. Topography will be disabled."
        fi
    fi
fi

# -- [6/9] Patch model.conf --
log "[6/9] Patching model.conf"
MODEL_CONF="${PROFILE_CONFIG_DIR}/model.conf"

if [ -f "${MODEL_CONF}" ]; then
    # Remove any stale CA_vs30.grd reference
    if grep -q "CA_vs30.grd" "${MODEL_CONF}" 2>/dev/null; then
        sed -i 's|CA_vs30\.grd||g' "${MODEL_CONF}"
        log "  Removed CA_vs30.grd reference."
    fi

    # Set vs30file
    if [ -n "${VS30_FILE}" ]; then
        # Check if vs30file key exists in model.conf
        if grep -q "vs30file" "${MODEL_CONF}" 2>/dev/null; then
            sed -i "s|^[[:space:]]*vs30file[[:space:]]*=.*|    vs30file = ${VS30_FILE}|" "${MODEL_CONF}"
        else
            # Add vs30file under [data] section if it exists, otherwise append
            if grep -q '^\[data\]' "${MODEL_CONF}" 2>/dev/null; then
                sed -i "/^\[data\]/a\\    vs30file = ${VS30_FILE}" "${MODEL_CONF}"
            else
                printf '\n[data]\n    vs30file = %s\n' "${VS30_FILE}" >> "${MODEL_CONF}"
            fi
        fi
        log "  Set vs30file = ${VS30_FILE}"
    elif [ "${ALLOW_UNIFORM_VS30}" = "1" ]; then
        # Explicitly clear vs30file
        if grep -q "vs30file" "${MODEL_CONF}" 2>/dev/null; then
            sed -i "s|^[[:space:]]*vs30file[[:space:]]*=.*|    vs30file = |" "${MODEL_CONF}"
        fi
        log "  Cleared vs30file (uniform VS30 allowed)."
    fi
else
    fail "model.conf not found at ${MODEL_CONF}"
fi

# -- [7/9] Patch products.conf --
log "[7/9] Patching products.conf"
PRODUCTS_CONF="${PROFILE_CONFIG_DIR}/products.conf"

if [ -f "${PRODUCTS_CONF}" ]; then
    if [ -n "${TOPO_FILE}" ]; then
        if grep -q "topography" "${PRODUCTS_CONF}" 2>/dev/null; then
            sed -i "s|^[[:space:]]*topography[[:space:]]*=.*|    topography = ${TOPO_FILE}|" "${PRODUCTS_CONF}"
        else
            if grep -q '^\[mapping\]' "${PRODUCTS_CONF}" 2>/dev/null; then
                sed -i "/^\[mapping\]/a\\    topography = ${TOPO_FILE}" "${PRODUCTS_CONF}"
            else
                printf '\n[mapping]\n    topography = %s\n' "${TOPO_FILE}" >> "${PRODUCTS_CONF}"
            fi
        fi
        log "  Set topography = ${TOPO_FILE}"
    else
        # Disable topography if no grid available
        if grep -q "topography" "${PRODUCTS_CONF}" 2>/dev/null; then
            sed -i "s|^[[:space:]]*topography[[:space:]]*=.*|    topography = |" "${PRODUCTS_CONF}"
            log "  Disabled topography (no grid available)."
        fi
    fi
else
    log "  products.conf not found at ${PRODUCTS_CONF}; skipping topo patch."
fi

# -- [8/9] Readiness probes --
log "[8/9] Running readiness probes"

# Probe 1: shake --version
if shake --version >/dev/null 2>&1; then
    log "  shake --version: OK"
else
    fail "shake --version failed"
fi

# Probe 2: model.conf parseable (Python validation)
if [ -f "${MODEL_CONF}" ]; then
    if python3 -c "
from configobj import ConfigObj
conf = ConfigObj('${MODEL_CONF}')
print('model.conf: parseable (' + str(len(conf)) + ' sections)')
" 2>/dev/null; then
        log "  model.conf parse: OK"
    else
        fail "model.conf is not parseable"
    fi
else
    fail "model.conf not found at ${MODEL_CONF}"
fi

# Probe 3: profiles.conf exists, parseable, and lists active profile
if [ -f "${PROFILES_CONF}" ]; then
    if python3 -c "
from configobj import ConfigObj
conf = ConfigObj('${PROFILES_CONF}')
print('profiles.conf: parseable (' + str(len(conf)) + ' profiles)')
" 2>/dev/null; then
        if grep -q "${PROFILE}" "${PROFILES_CONF}" 2>/dev/null; then
            log "  profiles.conf: OK (parseable, lists profile '${PROFILE}')"
        else
            fail "profiles.conf does not list active profile '${PROFILE}'"
        fi
    else
        fail "profiles.conf is not parseable"
    fi
else
    fail "profiles.conf not found at ${PROFILES_CONF}"
fi

# Probe 4: Profile config directory exists
if [ -d "${PROFILE_CONFIG_DIR}" ]; then
    log "  Profile config dir: OK (${PROFILE_CONFIG_DIR})"
else
    fail "Profile config directory not found at ${PROFILE_CONFIG_DIR}"
fi

# Probe 5: Profile data symlink resolves to SERVICE_ROOT/work
if [ -L "${PROFILE_DATA_DIR}" ]; then
    ACTUAL_TARGET="$(readlink -f "${PROFILE_DATA_DIR}" 2>/dev/null || readlink "${PROFILE_DATA_DIR}")"
    EXPECTED_TARGET="$(readlink -f "${SERVICE_ROOT}/.service/work" 2>/dev/null || echo "${SERVICE_ROOT}/.service/work")"
    if [ "${ACTUAL_TARGET}" = "${EXPECTED_TARGET}" ]; then
        log "  Profile data symlink: OK (${PROFILE_DATA_DIR} -> ${SERVICE_ROOT}/.service/work)"
    else
        fail "Profile data symlink points to ${ACTUAL_TARGET}, expected ${EXPECTED_TARGET}"
    fi
elif [ -d "${PROFILE_DATA_DIR}" ]; then
    fail "Profile data dir is a directory, not a symlink (expected symlink to ${SERVICE_ROOT}/.service/work)"
else
    fail "Profile data dir not found at ${PROFILE_DATA_DIR}"
fi

# Probe 6: vs30file path resolves (after model.conf patching)
if [ -n "${VS30_FILE}" ]; then
    if [ -f "${VS30_FILE}" ]; then
        VS30_SIZE="$(stat -c '%s' "${VS30_FILE}" 2>/dev/null || stat -f '%z' "${VS30_FILE}" 2>/dev/null || echo "0")"
        if [ "${VS30_SIZE}" -gt 0 ] 2>/dev/null; then
            log "  VS30 file: OK (${VS30_FILE}, ${VS30_SIZE} bytes)"
        else
            fail "VS30 file exists but is empty: ${VS30_FILE}"
        fi
    else
        fail "VS30 file configured but does not exist: ${VS30_FILE}"
    fi
elif [ "${ALLOW_UNIFORM_VS30}" = "1" ]; then
    log "  VS30 file: OVERRIDE ACTIVE -- uniform VS30 (760 m/s) allowed by operator (SHAKEMAP_ALLOW_UNIFORM_VS30=1)"
    log "  WARNING: This is a development/emergency override. Production deployments should provide a VS30 grid."
else
    fail "No VS30 grid file found and uniform VS30 not allowed. Provide VS30 data or set SHAKEMAP_ALLOW_UNIFORM_VS30=1"
fi

# Probe 7: products.conf parseable (if it exists)
if [ -f "${PRODUCTS_CONF}" ]; then
    if python3 -c "
from configobj import ConfigObj
conf = ConfigObj('${PRODUCTS_CONF}')
print('products.conf: parseable (' + str(len(conf)) + ' sections)')
" 2>/dev/null; then
        log "  products.conf parse: OK"
    else
        fail "products.conf is not parseable"
    fi
else
    log "  products.conf: not present (optional, skipping)"
fi

# -- [9/9] Write sentinel --
log "[9/9] Writing Stage 2 sentinel"
mkdir -p "$(dirname "${SENTINEL}")"

if [ -n "${ERRORS}" ]; then
    echo "not_ready|${ERRORS}" > "${SENTINEL}"
    log "Stage 2 result: NOT READY -- ${ERRORS}"
    log "Sentinel written to ${SENTINEL}"
    # Exit 0 -- let the service keep running; /healthz will report not_ready
    exit 0
else
    # Record override flags in sentinel so /healthz and /config can report them
    if [ "${ALLOW_UNIFORM_VS30}" = "1" ] && [ -z "${VS30_FILE}" ]; then
        echo "ready|uniform_vs30_override" > "${SENTINEL}"
        log "Stage 2 result: READY (with uniform VS30 override)"
    else
        echo "ready" > "${SENTINEL}"
        log "Stage 2 result: READY"
    fi
    log "Sentinel written to ${SENTINEL}"
    exit 0
fi

