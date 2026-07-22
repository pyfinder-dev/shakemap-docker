#!/usr/bin/env bash
# Prepare and validate the mounted ShakeMap runtime before service startup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"
IMAGE="shakemap-docker:latest"
NO_DOWNLOAD=0
VS30_SOURCE=""
TOPO_SOURCE=""
SLAB_SOURCE=""

usage() {
    sed -n '2,22p' "$0" | sed 's/^# *//'
}

# Usage:
#   ./scripts/configure-shakemap.sh [OPTIONS]
#
# Options:
#   --runtime DIR       Mounted runtime root (default: ./runtime)
#   --image IMAGE       Built image to use (default: shakemap-docker:latest)
#   --vs30-source FILE  Manually placed global_vs30.grd
#   --topo-source FILE  Manually placed topo_30sec.grd
#   --slab-source FILE  Manually placed official slab2.zip
#   --no-download       Fail rather than download missing/invalid source data
#   --help              Show this help

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime) RUNTIME_DIR="${2:?--runtime requires a directory}"; shift 2 ;;
        --image) IMAGE="${2:?--image requires an image}"; shift 2 ;;
        --vs30-source) VS30_SOURCE="${2:?--vs30-source requires a file}"; shift 2 ;;
        --topo-source) TOPO_SOURCE="${2:?--topo-source requires a file}"; shift 2 ;;
        --slab-source) SLAB_SOURCE="${2:?--slab-source requires a file}"; shift 2 ;;
        --no-download) NO_DOWNLOAD=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done

PYTHON_BIN="${SHAKEMAP_PREPARE_PYTHON:-python}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: project Python is not active; run: source ../.venv/bin/activate" >&2
    exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not on PATH" >&2
    exit 2
fi
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "ERROR: built image not found: ${IMAGE}" >&2
    exit 2
fi

PACKAGE="${RUNTIME_DIR}/shakemap/data/test/v4.4.9"
PACKAGE_ARGS=(prepare --destination "${PACKAGE}" --migrate-known-legacy)
"${PYTHON_BIN}" "${PROJECT_ROOT}/scripts/prepare-shakemap-verification-data.py" "${PACKAGE_ARGS[@]}"

ARGS=(
    -m shakemap_service.preparation prepare
    --runtime "${RUNTIME_DIR}"
    --image "${IMAGE}"
    --fixture "${PROJECT_ROOT}/tests/fixtures/shakemap_scenario"
    --california-package "${PACKAGE}"
)
[[ -n "${VS30_SOURCE}" ]] && ARGS+=(--vs30-source "${VS30_SOURCE}")
[[ -n "${TOPO_SOURCE}" ]] && ARGS+=(--topo-source "${TOPO_SOURCE}")
[[ -n "${SLAB_SOURCE}" ]] && ARGS+=(--slab-source "${SLAB_SOURCE}")
[[ "${NO_DOWNLOAD}" = "1" ]] && ARGS+=(--no-download)

cd "${PROJECT_ROOT}"
exec "${PYTHON_BIN}" "${ARGS[@]}"
