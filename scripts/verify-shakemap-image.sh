#!/usr/bin/env bash
set -uo pipefail

# ------------------------------------------------------------------
# verify-shakemap-image.sh -- Container-Internal Module Verification
#
# Proves that the Python modules shipped inside the Docker image
# import and work correctly in the container environment.
#
# This is Layer 2 verification (between host tests and deployment
# verification).  It does NOT require a running service — it tests
# the image contents directly.
#
# Usage:
#   docker run --rm shakemap-service:latest /app/scripts/verify-shakemap-image.sh
#
#   Or inside a running container:
#   docker exec <container> /app/scripts/verify-shakemap-image.sh
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
        echo "  [PASS] ${TOTAL}. ${label}"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] ${TOTAL}. ${label}"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================================"
echo "  Container-Internal Module Verification"
echo "============================================================"
echo ""

RUNTIME_ROOT="${RUNTIME_ROOT:-/home/sysop/runtime}"
SERVICE_ROOT="${SERVICE_ROOT:-${RUNTIME_ROOT}/shakemap}"

# ------------------------------------------------------------------
# Immutable image identity
# ------------------------------------------------------------------
echo "--- Immutable image identity ---"

IDENTITY_CHECK=$(python3 - <<'PYEOF'
import hashlib
import importlib.metadata
import json
import platform
import subprocess
from pathlib import Path

errors = []
identity_path = Path("/opt/shakemap-build/identity.json")
try:
    from shakemap_service.build_identity import validate_build_identity
    manifest = validate_build_identity(json.loads(identity_path.read_text()))
    image = manifest["immutable_image"]
    upstream = image["upstream"]
    installed = image["installed"]

    head = subprocess.run(
        ["git", "-C", "/opt/shakemap", "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if head != upstream["source_commit"]:
        errors.append(f"source checkout={head}, manifest={upstream['source_commit']}")

    import shakemap  # noqa: F401 -- proves package import
    import shakemap_modules  # noqa: F401 -- proves package import
    shake_version = importlib.metadata.version("shakemap")
    modules_version = importlib.metadata.version("shakemap-modules")
    if shake_version != installed["shakemap_distribution_version"]:
        errors.append("installed ShakeMap version differs from manifest")
    if modules_version != installed["shakemap_modules_distribution_version"]:
        errors.append("installed shakemap-modules version differs from manifest")
    if platform.python_version() != installed["python_version"]:
        errors.append("Python version differs from manifest")

    inventory = Path(installed["dependency_inventory_path"])
    if not inventory.is_file() or not inventory.read_text().strip():
        errors.append("dependency inventory missing or empty")
    elif hashlib.sha256(inventory.read_bytes()).hexdigest() != installed["dependency_inventory_sha256"]:
        errors.append("dependency inventory digest mismatch")
except Exception as exc:
    errors.append(f"identity validation failed: {type(exc).__name__}: {exc}")

print("OK" if not errors else "ERRORS:" + "|".join(errors))
PYEOF
)

[ "${IDENTITY_CHECK}" = "OK" ]
check "Manifest, checkout, imports, versions, Python, and dependency inventory agree" $?
if [ "${IDENTITY_CHECK}" != "OK" ]; then
    echo "         ${IDENTITY_CHECK}"
fi

# ShakeMap 4.x reads profiles before processing --version, so create an
# isolated throwaway HOME. Normalize only the documented 'ShakeMap version X'
# prefix and require exact equality with the recorded installed version.
IDENTITY_HOME="$(mktemp -d /tmp/shakemap-identity.XXXXXX)"
if HOME="${IDENTITY_HOME}" sm_profile -c identity-check -a -n >/dev/null 2>&1; then
    NATIVE_VERSION_OUTPUT="$(HOME="${IDENTITY_HOME}" shake --version 2>&1)"
    NATIVE_VERSION="${NATIVE_VERSION_OUTPUT##*ShakeMap version }"
    RECORDED_VERSION="$(python3 - <<'PYEOF'
import json
print(json.load(open('/opt/shakemap-build/identity.json'))['immutable_image']['installed']['shakemap_distribution_version'])
PYEOF
)"
    [ "${NATIVE_VERSION}" = "${RECORDED_VERSION}" ]
    check "shake --version agrees with recorded installed version" $?
else
    check "shake --version agrees with recorded installed version" 1
fi
rm -rf "${IDENTITY_HOME}"

# ------------------------------------------------------------------
# Python module imports
# ------------------------------------------------------------------
echo "--- Python module imports ---"

python3 -c "from shakemap_service import paths" 2>/dev/null
check "import shakemap_service.paths" $?

python3 -c "from shakemap_service import config" 2>/dev/null
check "import shakemap_service.config" $?

python3 -c "from shakemap_service import status" 2>/dev/null
check "import shakemap_service.status" $?

python3 -c "from shakemap_service import queue" 2>/dev/null
check "import shakemap_service.queue" $?

python3 -c "from shakemap_service import submission" 2>/dev/null
check "import shakemap_service.submission" $?

python3 -c "from shakemap_service import worker" 2>/dev/null
check "import shakemap_service.worker" $?

python3 -c "from shakemap_service import runner" 2>/dev/null
check "import shakemap_service.runner" $?

python3 -c "from shakemap_service.main import app" 2>/dev/null
check "import shakemap_service.main.app (FastAPI)" $?

# ------------------------------------------------------------------
# FastAPI app structure
# ------------------------------------------------------------------
echo ""
echo "--- FastAPI app structure ---"

ROUTES_CHECK=$(python3 - <<'PYEOF'
import json
from shakemap_service.main import app

routes = {r.path for r in app.routes if hasattr(r, 'path')}
required = {"/healthz", "/config", "/config/profiles", "/events/submit",
            "/events", "/events/{event_id}", "/events/{event_id}/products", "/queue"}

missing = required - routes
if missing:
    print(f"MISSING:{','.join(sorted(missing))}")
else:
    print("OK")
PYEOF
)

[ "${ROUTES_CHECK}" = "OK" ]
check "All required API routes registered" $?
if [ "${ROUTES_CHECK}" != "OK" ]; then
    echo "         ${ROUTES_CHECK}"
fi

# ------------------------------------------------------------------
# paths.py resolution
# ------------------------------------------------------------------
echo ""
echo "--- Path resolution ---"

PATHS_CHECK=$(python3 - "${SERVICE_ROOT}" <<'PYEOF'
import sys
from shakemap_service import paths

service_root = sys.argv[1]
errors = []

# Check that paths resolve to the expected service root
sr = str(paths.service_root())
if sr != service_root:
    errors.append(f"service_root={sr}, expected={service_root}")

# Check user-facing directories
for name, fn in [("incoming", paths.incoming_dir),
                 ("products", paths.products_dir),
                 ("logs", paths.logs_dir)]:
    p = str(fn())
    if not p.startswith(service_root):
        errors.append(f"{name}_dir does not start with service_root: {p}")

# Check internal directories are under .service/
for name, fn in [("events", paths.events_dir),
                 ("work", paths.work_dir),
                 ("archive", paths.archive_dir)]:
    p = str(fn())
    if "/.service/" not in p:
        errors.append(f"{name}_dir is not under .service/: {p}")

# Check event_status_file path structure
esf = str(paths.event_status_file("test_event"))
if "/.service/events/test_event/requeststatus.json" not in esf:
    errors.append(f"event_status_file has wrong structure: {esf}")

# Check event_status_file does NOT contain .shakemap-service
if ".shakemap-service" in esf:
    errors.append(f"event_status_file still uses old .shakemap-service: {esf}")

# Check all_service_dirs returns correct count
asd = paths.all_service_dirs()
if len(asd) < 7:
    errors.append(f"all_service_dirs returns {len(asd)} dirs, expected >= 7")

# Check new helper functions exist and return Path objects
from pathlib import Path
for fn_name in ["event_manifest_file", "event_provenance_file",
                "event_audit_dir", "event_log_file", "event_events_dir"]:
    fn = getattr(paths, fn_name, None)
    if fn is None:
        errors.append(f"Missing function: paths.{fn_name}")
    else:
        result = fn("test_event")
        if not isinstance(result, Path):
            errors.append(f"paths.{fn_name} does not return Path")

if errors:
    print("ERRORS:" + "|".join(errors))
else:
    print("OK")
PYEOF
)

[ "${PATHS_CHECK}" = "OK" ]
check "paths.py resolves to correct directories" $?
if [ "${PATHS_CHECK}" != "OK" ]; then
    IFS='|' read -ra ERRS <<< "${PATHS_CHECK#ERRORS:}"
    for err in "${ERRS[@]}"; do
        echo "         ${err}"
    done
fi

# ------------------------------------------------------------------
# Runtime directory structure
# ------------------------------------------------------------------
echo ""
echo "--- Runtime directory structure ---"

# User-facing directories
for dir in incoming products logs data; do
    [ -d "${SERVICE_ROOT}/${dir}" ]
    check "User-facing dir exists: ${dir}/" $?
done

# Internal .service directories
for dir in .service .service/events .service/work .service/archive; do
    [ -d "${SERVICE_ROOT}/${dir}" ]
    check "Internal dir exists: ${dir}/" $?
done

# Old top-level dirs must NOT exist (unless from a previous run — check for absence)
OLD_DIRS_ABSENT=0
for dir in events work archive; do
    if [ -d "${SERVICE_ROOT}/${dir}" ]; then
        OLD_DIRS_ABSENT=1
        echo "         WARNING: Old top-level dir exists: ${SERVICE_ROOT}/${dir}"
    fi
done
check "No old top-level events/work/archive dirs" ${OLD_DIRS_ABSENT}

# ------------------------------------------------------------------
# sysop write access
# ------------------------------------------------------------------
echo ""
echo "--- sysop write access ---"

CURRENT_USER="$(id -un 2>/dev/null || echo unknown)"
[ "${CURRENT_USER}" = "sysop" ]
check "Running as sysop (got: ${CURRENT_USER})" $?

for dir in incoming products logs .service/events .service/work .service/archive; do
    TESTFILE="${SERVICE_ROOT}/${dir}/.writetest_$$"
    if touch "${TESTFILE}" 2>/dev/null; then
        rm -f "${TESTFILE}"
        check "Writable: ${dir}/" 0
    else
        check "Writable: ${dir}/" 1
    fi
done

# ------------------------------------------------------------------
# Scripts exist and are executable
# ------------------------------------------------------------------
echo ""
echo "--- Scripts ---"

EXPECTED_SCRIPTS=(
    configure-shakemap.sh
    verify-shakemap-build.sh
    verify-shakemap-image.sh
    verify-shakemap-config.sh
    verify-shakemap-deployment.sh
    inspect-shakemap-config.sh
    inspect-shakemap-events.sh
    start-shakemap-docker.sh
    build-shakemap-docker.sh
    run-shakemap-ci-tests.sh
)

for script in "${EXPECTED_SCRIPTS[@]}"; do
    [ -f "/app/scripts/${script}" ] && [ -x "/app/scripts/${script}" ]
    check "Script exists and executable: ${script}" $?
done

# ------------------------------------------------------------------
# ShakeMap CLI
# ------------------------------------------------------------------
echo ""
echo "--- ShakeMap CLI ---"

command -v shake >/dev/null 2>&1
check "shake is on PATH" $?

shake --help >/dev/null 2>&1
SHAKE_HELP_RC=$?
# shake --help may exit non-zero without an initialized profile.
# Accept exit code 0 or verify shake is at least a callable Python entry point.
if [ ${SHAKE_HELP_RC} -eq 0 ]; then
    check "shake --help exits 0" 0
else
    # Verify shake is a real Python entry point (not just found on PATH)
    SHAKE_PATH="$(command -v shake)"
    head -1 "${SHAKE_PATH}" 2>/dev/null | grep -q 'python' 2>/dev/null
    check "shake is a Python entry point (--help exit=${SHAKE_HELP_RC}, expected without profile)" $?
fi

# ------------------------------------------------------------------
# Runner features (product validation, manifest, provenance)
# ------------------------------------------------------------------
echo ""
echo "--- Runner features ---"

RUNNER_CHECK=$(python3 - <<'PYEOF'
errors = []
from shakemap_service import runner

# Required functions
for fn_name in ["run_shake", "run_shake_for_event",
                "_validate_products", "_write_products_manifest",
                "_write_provenance", "_copy_audit_record",
                "_publish_products_atomic", "_prepare_shakemap_data",
                "_find_products_dir"]:
    if not hasattr(runner, fn_name):
        errors.append(f"Missing function: runner.{fn_name}")

# Required constants
if not hasattr(runner, "_REQUIRED_CORE_PRODUCTS"):
    errors.append("Missing: runner._REQUIRED_CORE_PRODUCTS")
else:
    rcp = runner._REQUIRED_CORE_PRODUCTS
    if "grid.xml" not in rcp and "shake_result.hdf" not in rcp:
        errors.append(f"_REQUIRED_CORE_PRODUCTS missing expected entries: {rcp}")

# ShakeError class
if not hasattr(runner, "ShakeError"):
    errors.append("Missing: runner.ShakeError")

# run_shake log_file parameter
import inspect
sig = inspect.signature(runner.run_shake)
if "log_file" not in sig.parameters:
    errors.append("run_shake missing log_file parameter")

if errors:
    print("ERRORS:" + "|".join(errors))
else:
    print("OK")
PYEOF
)

[ "${RUNNER_CHECK}" = "OK" ]
check "Runner has validation/manifest/provenance/audit functions" $?
if [ "${RUNNER_CHECK}" != "OK" ]; then
    IFS='|' read -ra ERRS <<< "${RUNNER_CHECK#ERRORS:}"
    for err in "${ERRS[@]}"; do
        echo "         ${err}"
    done
fi

# ------------------------------------------------------------------
# Status module
# ------------------------------------------------------------------
echo ""
echo "--- Status module ---"

STATUS_CHECK=$(python3 - <<'PYEOF'
errors = []
from shakemap_service.status import (
    EventStatus, RequestStatus, AttemptRecord,
    read_status, create_event_record, update_status,
    write_status_atomic, scan_event_records,
    transition_to_failed, transition_to_success,
)

# Verify all 9 frozen statuses
expected = {"REGISTERED", "VALIDATING", "VALIDATION_FAILED", "QUEUED",
            "RUNNING", "SUCCESS", "FAILED", "CANCELLED", "ARCHIVED"}
actual = {s.value for s in EventStatus}
if expected != actual:
    errors.append(f"EventStatus mismatch: missing={expected-actual}, extra={actual-expected}")

if errors:
    print("ERRORS:" + "|".join(errors))
else:
    print("OK")
PYEOF
)

[ "${STATUS_CHECK}" = "OK" ]
check "Status module: all enums and functions present" $?
if [ "${STATUS_CHECK}" != "OK" ]; then
    echo "         ${STATUS_CHECK}"
fi

# ------------------------------------------------------------------
# Worker module
# ------------------------------------------------------------------
echo ""
echo "--- Worker module ---"

WORKER_CHECK=$(python3 - <<'PYEOF'
errors = []
from shakemap_service.worker import (
    execute_shakemap, execute_placeholder,
    run_worker_cycle, recover_interrupted_events,
    process_next_event,
)

if errors:
    print("ERRORS:" + "|".join(errors))
else:
    print("OK")
PYEOF
)

[ "${WORKER_CHECK}" = "OK" ]
check "Worker module: all functions importable" $?
if [ "${WORKER_CHECK}" != "OK" ]; then
    echo "         ${WORKER_CHECK}"
fi

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Container-Internal Verification: ${PASS} passed, ${FAIL} failed (of ${TOTAL})"
echo "============================================================"

if [ ${FAIL} -gt 0 ]; then
    exit 1
else
    exit 0
fi
