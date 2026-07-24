#!/usr/bin/env bash
# -------------------------------------------------------------------
# build-shakemap-docker.sh -- Build the ShakeMap Docker image locally.
#
# Usage:
#   ./scripts/build-shakemap-docker.sh [OPTIONS]
#
# Options:
#   --tag TAG           Image name:tag  (default: shakemap-docker:latest)
#   --platform PLAT     Target platform (default: current docker default)
#   --no-cache          Build without layer cache
#   --release-tag TAG   Official final stable upstream tag override
#   --help              Show this help message
#
# Examples:
#   ./scripts/build-shakemap-docker.sh
#   ./scripts/build-shakemap-docker.sh --tag shakemap-docker:test
#   ./scripts/build-shakemap-docker.sh --release-tag v4.4.9
#   ./scripts/build-shakemap-docker.sh --platform linux/amd64
#   ./scripts/build-shakemap-docker.sh --no-cache
# -------------------------------------------------------------------
set -euo pipefail

# -- Defaults --
IMAGE_TAG="shakemap-docker:latest"
PLATFORM=""
NO_CACHE=""
RELEASE_TAG_OVERRIDE=""

# -- Parse arguments --
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --tag requires a value (e.g. --tag shakemap-docker:test)" >&2
                exit 1
            fi
            IMAGE_TAG="$2"
            shift 2
            ;;
        --platform)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --platform requires a value (e.g. --platform linux/amd64)" >&2
                exit 1
            fi
            PLATFORM="$2"
            shift 2
            ;;
        --no-cache)
            NO_CACHE="yes"
            shift
            ;;
        --release-tag)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --release-tag requires a value" >&2
                exit 1
            fi
            RELEASE_TAG_OVERRIDE="$2"
            shift 2
            ;;
        --help|-h)
            awk '/^# ---/{if(n++)exit;next} n&&/^#/{sub(/^# ?/,"");print}' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option: $1" >&2
            echo "Run with --help for usage." >&2
            exit 1
            ;;
    esac
done

# [1/4] Locate the build context (repo root = parent of scripts/)
echo "[1/4] Locating Dockerfile"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_CONTEXT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

if [[ ! -f "${BUILD_CONTEXT}/Dockerfile" ]]; then
    echo "ERROR: Cannot find Dockerfile at ${BUILD_CONTEXT}/Dockerfile" >&2
    exit 1
fi
echo "  Build context: ${BUILD_CONTEXT}"

# [2/4] Resolve the official stable release once, before Docker starts.
echo "[2/4] Resolving official stable USGS ShakeMap release"
PYTHON_BIN="${SHAKEMAP_HOST_PYTHON:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: Python 3.10 or newer is required; interpreter not found: ${PYTHON_BIN}" >&2
    echo "Set SHAKEMAP_HOST_PYTHON to a supported interpreter path if needed." >&2
    exit 1
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ERROR: ${PYTHON_BIN} must be Python 3.10 or newer; no third-party host packages are required." >&2
    exit 1
fi

RESOLVE_ARGS=(-m shakemap_service.release resolve)
if [[ -n "${RELEASE_TAG_OVERRIDE}" ]]; then
    RESOLVE_ARGS+=(--release-tag "${RELEASE_TAG_OVERRIDE}")
fi

RESOLUTION_OUTPUT="$(cd "${BUILD_CONTEXT}" && "${PYTHON_BIN}" "${RESOLVE_ARGS[@]}")"
RESOLUTION_LINES=()
while IFS= read -r line; do
    RESOLUTION_LINES+=("${line}")
done <<< "${RESOLUTION_OUTPUT}"
if [[ "${#RESOLUTION_LINES[@]}" -ne 3 ]]; then
    echo "ERROR: Release resolver returned malformed output." >&2
    exit 1
fi
SHAKEMAP_RELEASE_TAG="${RESOLUTION_LINES[0]}"
SHAKEMAP_SOURCE_COMMIT="${RESOLUTION_LINES[1]}"
SHAKEMAP_SOURCE_URL="${RESOLUTION_LINES[2]}"
SHAKEMAP_RELEASE_VERSION="${SHAKEMAP_RELEASE_TAG#v}"

echo "  Release tag: ${SHAKEMAP_RELEASE_TAG}"

# [3/4] Verify docker is available
echo "[3/4] Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not in PATH." >&2
    exit 1
fi
BUILD_TIMESTAMP_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# [4/4] Assemble a shell-safe ordinary Docker build command.
CMD=(
    docker build
    --quiet
    --tag "${IMAGE_TAG}"
)
if [[ -n "${PLATFORM}" ]]; then
    CMD+=(--platform "${PLATFORM}")
fi
if [[ -n "${NO_CACHE}" ]]; then
    CMD+=(--no-cache)
fi
CMD+=(
    --build-arg "SHAKEMAP_SOURCE_URL=${SHAKEMAP_SOURCE_URL}"
    --build-arg "SHAKEMAP_RELEASE_TAG=${SHAKEMAP_RELEASE_TAG}"
    --build-arg "SHAKEMAP_RELEASE_VERSION=${SHAKEMAP_RELEASE_VERSION}"
    --build-arg "SHAKEMAP_SOURCE_COMMIT=${SHAKEMAP_SOURCE_COMMIT}"
    --build-arg "BUILD_TIMESTAMP_UTC=${BUILD_TIMESTAMP_UTC}"
    "${BUILD_CONTEXT}"
)

echo "[4/4] Building image '${IMAGE_TAG}'"

"${CMD[@]}" >/dev/null
echo "Image '${IMAGE_TAG}' built successfully."
