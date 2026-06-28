#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# Stage 1 -- Build / Runtime Foundation
#
# This entrypoint handles ONLY Stage 1 concerns:
#   [1/7] Read environment variables with defaults
#   [2/7] Log environment
#   [3/7] Ensure runtime root exists
#   [4/7] Optional mount check (REQUIRE_MOUNT)
#   [5/7] Create service directories and verify permissions
#   [6/7] Verify ShakeMap CLI is on PATH (smoke check)
#   [7/7] Start FastAPI service
#
# Stage 2 (ShakeMap profile configuration, data provisioning) is
# handled separately by /app/scripts/configure-shakemap.sh, run by
# the operator after the container is up.
# ------------------------------------------------------------------

# [1/7] Read environment with defaults
RUNTIME_ROOT="${RUNTIME_ROOT:-/home/sysop/runtime}"
SERVICE_ROOT="${SERVICE_ROOT:-${RUNTIME_ROOT}/shakemap}"
PROFILE="${SHAKEMAP_PROFILE:-default}"
REQUIRE_MOUNT="${SHAKEMAP_REQUIRE_MOUNT:-0}"
PORT="${SHAKEMAP_PORT:-9010}"
MODULES="${SHAKEMAP_MODULES:-select assemble model contour mapping stations gridxml}"

# [2/7] Log environment
echo "[entrypoint] Starting ShakeMap Docker entrypoint (Stage 1)..."
echo "[entrypoint] RUNTIME_ROOT            = ${RUNTIME_ROOT}"
echo "[entrypoint] SERVICE_ROOT            = ${SERVICE_ROOT}"
echo "[entrypoint] SHAKEMAP_PROFILE        = ${PROFILE}"
echo "[entrypoint] SHAKEMAP_PORT           = ${PORT}"
echo "[entrypoint] SHAKEMAP_REQUIRE_MOUNT  = ${REQUIRE_MOUNT}"
echo "[entrypoint] SHAKEMAP_MODULES        = ${MODULES}"

# [3/7] Ensure runtime root exists
echo "[entrypoint] [3/7] Ensuring runtime root exists"
mkdir -p "${RUNTIME_ROOT}"

# [4/7] Optional safety: require that SERVICE_ROOT is a mount
if [ "${REQUIRE_MOUNT}" = "1" ]; then
    echo "[entrypoint] [4/7] Checking mount requirement"
    if ! grep -q " ${SERVICE_ROOT} " /proc/mounts; then
        echo "[entrypoint] ERROR: SHAKEMAP_REQUIRE_MOUNT=1 but ${SERVICE_ROOT} is not a mounted volume." >&2
        echo "[entrypoint] Please mount a host directory or named volume to ${SERVICE_ROOT}." >&2
        exit 1
    fi
else
    echo "[entrypoint] [4/7] Mount check skipped (REQUIRE_MOUNT=0)"
fi

# [5/7] Create service directories and verify permissions
#
# Layout:
#   User-facing (top-level):  incoming/ products/ logs/ data/
#   Internal (.service/):     .service/events/ .service/work/ .service/archive/
#
echo "[entrypoint] [5/7] Creating service directories and verifying permissions"

# User-facing directories
for dir in incoming products logs; do
    mkdir -p "${SERVICE_ROOT}/${dir}"
done

# Internal service state directories
for dir in .service/events .service/work .service/archive; do
    mkdir -p "${SERVICE_ROOT}/${dir}"
done

# Shared data directory structure for Stage 2
mkdir -p "${SERVICE_ROOT}/data/vs30"
mkdir -p "${SERVICE_ROOT}/data/topo"

# Create .shakemap dir for Stage 2 sentinel
mkdir -p "${HOME:-/home/sysop}/.shakemap"

# Best-effort chmod -- has NO real effect on bind mounts.
for dir in incoming products logs data data/vs30 data/topo .service .service/events .service/work .service/archive; do
    chmod 0755 "${SERVICE_ROOT}/${dir}" 2>/dev/null || true
done

# Verify sysop can write to all required directories.
for dir in incoming products logs .service/events .service/work .service/archive; do
    DIRPATH="${SERVICE_ROOT}/${dir}"
    if ! touch "${DIRPATH}/.writetest_$$" 2>/dev/null; then
        echo "" >&2
        echo "[entrypoint] ERROR: ${DIRPATH} is not writable." >&2
        echo "" >&2
        echo "  Directory:      ${DIRPATH}" >&2
        OWNER="$(stat -c '%u:%g' "${DIRPATH}" 2>/dev/null || stat -f '%u:%g' "${DIRPATH}" 2>/dev/null || echo 'unknown')"
        echo "  Current owner:  ${OWNER}" >&2
        echo "  Required owner: 1000:1000" >&2
        echo "" >&2
        echo "  Suggested fix:" >&2
        echo "    chown -R 1000:1000 <host-runtime-dir>" >&2
        echo "" >&2
        exit 1
    fi
    rm -f "${DIRPATH}/.writetest_$$"
done
echo "[entrypoint] All service directories writable by sysop (UID $(id -u))"

# [6/7] Verify ShakeMap CLI available (smoke check)
echo "[entrypoint] [6/7] Verifying ShakeMap CLI"
if command -v shake >/dev/null 2>&1; then
    echo "[entrypoint] ShakeMap CLI found: $(command -v shake)"
else
    echo "[entrypoint] WARNING: 'shake' not found on PATH. ShakeMap may not be installed correctly."
fi

# Stage 2 is NOT run by default
echo "[entrypoint] Stage 1 complete. Stage 2 configuration must be run separately:"
echo "[entrypoint]   docker exec <container> /app/scripts/configure-shakemap.sh"

# [7/7] Start the FastAPI service
echo "[entrypoint] [7/7] Starting shakemap_service on port ${PORT}"
cd /app
exec uvicorn shakemap_service.main:app --host 0.0.0.0 --port "${PORT}"
