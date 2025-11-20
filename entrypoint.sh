#!/usr/bin/env bash
set -euo pipefail

# ----- Read environment with defaults -----
DATA_ROOT="${SHAKEMAP_DATA_ROOT:-/data/shakemap}"
PROFILE="${SHAKEMAP_PROFILE:-default}"
REQUIRE_MOUNT="${SHAKEMAP_REQUIRE_MOUNT:-0}"
PORT="${SHAKEMAP_PORT:-9010}"

echo "[entrypoint] SHAKEMAP_DATA_ROOT = ${DATA_ROOT}"
echo "[entrypoint] SHAKEMAP_PROFILE   = ${PROFILE}"
echo "[entrypoint] SHAKEMAP_PORT      = ${PORT}"
echo "[entrypoint] SHAKEMAP_REQUIRE_MOUNT = ${REQUIRE_MOUNT}"

# ----- Ensure data root exists -----
mkdir -p "${DATA_ROOT}"

# ----- Optional safety: require that DATA_ROOT is a mount -----
if [ "${REQUIRE_MOUNT}" = "1" ]; then
    # Very simple check: look for ' DATA_ROOT ' in /proc/mounts
    if ! grep -q " ${DATA_ROOT} " /proc/mounts; then
        echo "[entrypoint] ERROR: SHAKEMAP_REQUIRE_MOUNT=1 but ${DATA_ROOT} is not a mounted volume." >&2
        echo "[entrypoint] Please mount a host directory or named volume to ${DATA_ROOT}." >&2
        exit 1
    fi
fi

PROFILE_DIR="${DATA_ROOT}/profiles/${PROFILE}"

# ----- Initialize ShakeMap profile if needed -----
if [ ! -d "${PROFILE_DIR}" ]; then
    echo "[entrypoint] Initializing ShakeMap profile '${PROFILE}' under ${DATA_ROOT}"
    cd "${DATA_ROOT}"
    # This should create profiles/ and default config under DATA_ROOT
    shake init
    shake select profile "${PROFILE}"
else
    echo "[entrypoint] Using existing ShakeMap profile directory: ${PROFILE_DIR}"
fi

# ----- Start the FastAPI service -----
cd /app
echo "[entrypoint] Starting shakemap_service on port ${PORT}"
exec uvicorn shakemap_service.main:app --host 0.0.0.0 --port "${PORT}"
