#!/usr/bin/env bash
# Provision pinned global data at the contracted mounted-runtime paths.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"
NO_DOWNLOAD=0
VS30_SOURCE=""
TOPO_SOURCE=""
SLAB_SOURCE=""

usage() {
    sed -n '17,26p' "$0" | sed 's/^# *//'
}

# Usage:
#   ./scripts/configure-shakemap.sh [OPTIONS]
#
# Options:
#   --runtime DIR       Mounted runtime root (default: ./runtime)
#   --vs30-source FILE  Manually placed global_vs30.grd
#   --topo-source FILE  Manually placed topo_30sec.grd
#   --slab-source FILE  Manually placed official slab2.zip
#   --no-download       Fail rather than download missing/invalid source data
#   --help              Show this help

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime) RUNTIME_DIR="${2:?--runtime requires a directory}"; shift 2 ;;
        --vs30-source) VS30_SOURCE="${2:?--vs30-source requires a file}"; shift 2 ;;
        --topo-source) TOPO_SOURCE="${2:?--topo-source requires a file}"; shift 2 ;;
        --slab-source) SLAB_SOURCE="${2:?--slab-source requires a file}"; shift 2 ;;
        --no-download) NO_DOWNLOAD=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done

PYTHON_BIN="${SHAKEMAP_HOST_PYTHON:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: Python 3.10 or newer is required; interpreter not found: ${PYTHON_BIN}" >&2
    echo "Set SHAKEMAP_HOST_PYTHON to a supported interpreter path if needed." >&2
    exit 2
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ERROR: ${PYTHON_BIN} must be Python 3.10 or newer; provisioning uses only the standard library on the host." >&2
    exit 2
fi

ARGS=(
    -m shakemap_service.preparation provision-global
    --data-root "${RUNTIME_DIR}/shakemap/data"
)
[[ -n "${VS30_SOURCE}" ]] && ARGS+=(--vs30-source "${VS30_SOURCE}")
[[ -n "${TOPO_SOURCE}" ]] && ARGS+=(--topo-source "${TOPO_SOURCE}")
[[ -n "${SLAB_SOURCE}" ]] && ARGS+=(--slab-source "${SLAB_SOURCE}")
[[ "${NO_DOWNLOAD}" = "1" ]] && ARGS+=(--no-download)

cd "${PROJECT_ROOT}"
exec "${PYTHON_BIN}" "${ARGS[@]}"
