#!/usr/bin/env bash
set -euo pipefail

# ----- Read environment with defaults -----
DATA_ROOT="${SHAKEMAP_DATA_ROOT:-/data/shakemap}"
PROFILE="${SHAKEMAP_PROFILE:-default}"
REQUIRE_MOUNT="${SHAKEMAP_REQUIRE_MOUNT:-0}"
PORT="${SHAKEMAP_PORT:-9010}"

echo "[entrypoint] SHAKEMAP_DATA_ROOT      = ${DATA_ROOT}"
echo "[entrypoint] SHAKEMAP_PROFILE        = ${PROFILE}"
echo "[entrypoint] SHAKEMAP_PORT           = ${PORT}"
echo "[entrypoint] SHAKEMAP_REQUIRE_MOUNT  = ${REQUIRE_MOUNT}"

# ----- Ensure data root exists -----
mkdir -p "${DATA_ROOT}"

# ----- Optional safety: require that DATA_ROOT is a mount -----
if [ "${REQUIRE_MOUNT}" = "1" ]; then
    if ! grep -q " ${DATA_ROOT} " /proc/mounts; then
        echo "[entrypoint] ERROR: SHAKEMAP_REQUIRE_MOUNT=1 but ${DATA_ROOT} is not a mounted volume." >&2
        echo "[entrypoint] Please mount a host directory or named volume to ${DATA_ROOT}." >&2
        exit 1
    fi
fi

# ----- Profile locations (for user 'shakemap') -----
# sm_profile docs: profiles.conf lives under $HOME/.shakemap,
# install/data dirs under $HOME/shake_profiles/<profile> by default.
HOME_DIR="${HOME:-/home/shakemap}"
# NOTE: sm_profile creates profiles under 'shakemap_profiles', not 'shake_profiles'
PROFILE_ROOT="${HOME_DIR}/shakemap_profiles/${PROFILE}"
PROFILE_DATA_DIR="${PROFILE_ROOT}/data"
PROFILE_INSTALL_DIR="${PROFILE_ROOT}/install"
PROFILES_CONF="${HOME_DIR}/.shakemap/profiles.conf"

echo "[entrypoint] HOME_DIR        = ${HOME_DIR}"
echo "[entrypoint] PROFILE_ROOT    = ${PROFILE_ROOT}"
echo "[entrypoint] PROFILE_DATA    = ${PROFILE_DATA_DIR}"
echo "[entrypoint] PROFILE_INSTALL = ${PROFILE_INSTALL_DIR}"

# ----- Ensure a ShakeMap profile exists -----
if [ ! -d "${PROFILE_ROOT}" ] || [ ! -f "${PROFILES_CONF}" ]; then
    echo "[entrypoint] Creating ShakeMap profile '${PROFILE}' via sm_profile..."
    # -c PROFILE : create and switch
    # -a         : accept defaults
    # -n         : skip topo & Vs30 grid downloads (user can handle later)
    sm_profile -c "${PROFILE}" -a -n
else
    echo "[entrypoint] Using existing ShakeMap profile '${PROFILE}'."
fi

# ----- Point the profile's data dir at our external DATA_ROOT -----
mkdir -p "${DATA_ROOT}"

if [ ! -L "${PROFILE_DATA_DIR}" ]; then
    if [ -e "${PROFILE_DATA_DIR}" ] && [ ! -L "${PROFILE_DATA_DIR}" ]; then
        echo "[entrypoint] Removing existing data dir at ${PROFILE_DATA_DIR} to replace with symlink."
        rmdir "${PROFILE_DATA_DIR}" 2>/dev/null || rm -rf "${PROFILE_DATA_DIR}"
    fi
    echo "[entrypoint] Linking ${PROFILE_DATA_DIR} -> ${DATA_ROOT}"
    ln -s "${DATA_ROOT}" "${PROFILE_DATA_DIR}"
else
    echo "[entrypoint] Data dir already symlinked: ${PROFILE_DATA_DIR} -> $(readlink -f "${PROFILE_DATA_DIR}")"
fi

# ----- Run 'shake init' only if profile not yet initialized -----
# 'shake init' populates install/config etc. We use presence of config dir as a sentinel.
if [ ! -d "${PROFILE_INSTALL_DIR}/config" ]; then
    echo "[entrypoint] Running 'shake init' for profile '${PROFILE}' (this may take a while the first time)..."
    shake init
else
    echo "[entrypoint] ShakeMap profile '${PROFILE}' already initialized; skipping 'shake init'."
fi

# ----- Start the FastAPI service -----
cd /app
echo "[entrypoint] Starting shakemap_service on port ${PORT}"
exec uvicorn shakemap_service.main:app --host 0.0.0.0 --port "${PORT}"
