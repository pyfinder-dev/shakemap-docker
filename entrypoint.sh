#!/usr/bin/env bash
set -euo pipefail

# Runtime foundation and API startup
#
# This entrypoint handles runtime/API concerns only:
#   [1/6] Read environment variables with defaults
#   [2/6] Log environment
#   [3/6] Ensure runtime root exists
#   [4/6] Create service directories and verify permissions
#   [5/6] Verify ShakeMap CLI is on PATH (smoke check)
#   [6/6] Start FastAPI service
#
# Runtime data is mounted read-only and managed outside this entrypoint.

# [1/6] Read environment with defaults
RUNTIME_ROOT="${RUNTIME_ROOT:-/home/sysop/runtime}"
SERVICE_ROOT="${RUNTIME_ROOT}/shakemap"
PORT="${SHAKEMAP_PORT:-9010}"

# The API remains the exec'd PID 1. Explicit shell records cover startup;
# Uvicorn's logging configuration owns persistence once the API takes over.
export RUNTIME_ROOT
mkdir -p "${RUNTIME_ROOT}"
log_startup() {
    python -m shakemap_service.service_logging service "${2:-INFO}" "$1"
}
startup_failed() {
    result=$?
    trap - ERR
    python -m shakemap_service.service_logging service ERROR \
        "[entrypoint] Startup failed with exit code ${result}. Inspect the preceding console diagnostic." >&2 || true
    exit "${result}"
}
trap startup_failed ERR

log_startup "[entrypoint] Starting ShakeMap Docker service..."
log_startup "[entrypoint] RUNTIME_ROOT = ${RUNTIME_ROOT}"
log_startup "[entrypoint] SERVICE_ROOT = ${SERVICE_ROOT}"
log_startup "[entrypoint] SHAKEMAP_PORT = ${PORT}"

# [4/6] Create service directories and verify permissions
#
# Layout:
#   User-facing (top-level):  products/ logs/
#   External read-only data:  data/
#   Internal (.service/):     .service/events/ .service/archive/
#
log_startup "[entrypoint] [4/6] Creating service directories and verifying permissions"

# User-facing directories
for dir in products logs data/inputs; do
    mkdir -p "${SERVICE_ROOT}/${dir}"
done

# Internal service state directories
for dir in .service/events .service/archive .service/queue; do
    mkdir -p "${SERVICE_ROOT}/${dir}"
done

# Verify sysop can write to all required directories.
for dir in products logs data/inputs .service/events .service/archive .service/queue; do
    DIRPATH="${SERVICE_ROOT}/${dir}"
    if ! touch "${DIRPATH}/.writetest_$$" 2>/dev/null; then
        log_startup "" >&2
        log_startup "[entrypoint] ERROR: ${DIRPATH} is not writable." ERROR >&2
        log_startup "" >&2
        log_startup "  Directory:      ${DIRPATH}" >&2
        OWNER="$(stat -c '%u:%g' "${DIRPATH}" 2>/dev/null || stat -f '%u:%g' "${DIRPATH}" 2>/dev/null || echo 'unknown')"
        log_startup "  Current owner:  ${OWNER}" >&2
        log_startup "  Required service access: UID:GID 1000:1000" >&2
        log_startup "" >&2
        log_startup "  Suggested fix:" >&2
        log_startup "    Stop the service, then run the host project's helper:" >&2
        log_startup "    sudo /path/to/shakemap-docker/scripts/repair-shakemap-writable-paths.sh --runtime-root <host-runtime-dir>" >&2
        log_startup "    Then rerun finalization normally." >&2
        log_startup "" >&2
        exit 1
    fi
    rm -f "${DIRPATH}/.writetest_$$"
done
log_startup "[entrypoint] All service directories writable by sysop (UID $(id -u))"

# [5/6] Verify ShakeMap CLI available (smoke check)
log_startup "[entrypoint] [5/6] Verifying ShakeMap CLI"
if command -v shake >/dev/null 2>&1; then
    log_startup "[entrypoint] ShakeMap CLI found: $(command -v shake)"
else
    log_startup "[entrypoint] WARNING: 'shake' not found on PATH. ShakeMap may not be installed correctly." WARNING
    log_startup "[entrypoint] Native calculations cannot run without 'shake'. Rebuild the image on the host: "\
"make build COMPONENT=shakemap in pyfinder-deploy, or make build in shakemap-docker." WARNING
    log_startup "[entrypoint] Then follow image verification and explicit container replacement/finalization in "\
"pyfinder-deploy/README.md or shakemap-docker/docs/quick-start.md. The API will still start for diagnostics." WARNING
fi

# [6/6] Start the FastAPI service
log_startup "[entrypoint] [6/6] Starting shakemap_service on port ${PORT}"
cd /app
exec uvicorn shakemap_service.main:app --host 0.0.0.0 --port "${PORT}" \
    --log-config /app/shakemap_service/data/service-logging.json
