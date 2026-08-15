#!/usr/bin/env bash
# Inspect, validate, or provision the two Stage 2 global data assets.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"
NO_DOWNLOAD=0
VS30_SOURCE=""
TOPO_SOURCE=""

usage() {
    sed -n '16,29p' "$0" | sed 's/^# *//'
}

# Usage:
#   ./scripts/manage-shakemap-data.sh ACTION [OPTIONS]
#
# Actions:
#   inspect             Report presence/readability without writing or hashing
#   validate            Validate pinned identities without writing
#   provision           Reuse valid assets or install only missing assets
#
# Options:
#   --runtime DIR       Mounted runtime root (default: ./runtime)
#   --vs30-source FILE  Manually placed global_vs30.grd
#   --topo-source FILE  Manually placed topo_30sec.grd
#   --no-download       Fail rather than download a missing asset
#   --help              Show this help

if [[ $# -eq 0 ]]; then
    usage >&2
    exit 2
fi

case "$1" in
    inspect|validate|provision) ACTION="$1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown action: $1" >&2; usage >&2; exit 2 ;;
esac

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime) RUNTIME_DIR="${2:?--runtime requires a directory}"; shift 2 ;;
        --vs30-source) VS30_SOURCE="${2:?--vs30-source requires a file}"; shift 2 ;;
        --topo-source) TOPO_SOURCE="${2:?--topo-source requires a file}"; shift 2 ;;
        --no-download) NO_DOWNLOAD=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ "${ACTION}" != "provision" ]] && \
   [[ -n "${VS30_SOURCE}" || -n "${TOPO_SOURCE}" || "${NO_DOWNLOAD}" = "1" ]]; then
    echo "ERROR: source and download options are valid only for provision" >&2
    exit 2
fi

if [[ -z "${VIRTUAL_ENV:-}" ]] || ! command -v python >/dev/null 2>&1; then
    echo "ERROR: activate the project Python environment before running this helper." >&2
    exit 2
fi
if ! python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) and sys.prefix != sys.base_prefix else 1)'; then
    echo "ERROR: the active python must be Python 3.10 or newer from a virtual environment." >&2
    exit 2
fi

ARGS=(
    -m shakemap_service.preparation "${ACTION}"
    --data-root "${RUNTIME_DIR}/shakemap/data"
)
[[ -n "${VS30_SOURCE}" ]] && ARGS+=(--vs30-source "${VS30_SOURCE}")
[[ -n "${TOPO_SOURCE}" ]] && ARGS+=(--topo-source "${TOPO_SOURCE}")
[[ "${NO_DOWNLOAD}" = "1" ]] && ARGS+=(--no-download)

cd "${PROJECT_ROOT}"
exec python "${ARGS[@]}"
