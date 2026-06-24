#!/usr/bin/env bash
# -------------------------------------------------------------------
# build-shakemap-docker.sh -- Build the ShakeMap Docker image locally.
#
# Usage:
#   ./scripts/build-shakemap-docker.sh [OPTIONS]
#
# Options:
#   --tag TAG           Image name:tag  (default: shakemap-service:latest)
#   --platform PLAT     Target platform (default: current docker default)
#   --no-cache          Build without layer cache
#   --help              Show this help message
#
# Examples:
#   ./scripts/build-shakemap-docker.sh
#   ./scripts/build-shakemap-docker.sh --tag shakemap-service:v1
#   ./scripts/build-shakemap-docker.sh --platform linux/amd64
#   ./scripts/build-shakemap-docker.sh --no-cache
# -------------------------------------------------------------------
set -euo pipefail

# -- Defaults --
IMAGE_TAG="shakemap-service:latest"
PLATFORM=""
NO_CACHE=""

# -- Parse arguments --
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --tag requires a value (e.g. --tag shakemap-service:v1)" >&2
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
BUILD_CONTEXT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ ! -f "${BUILD_CONTEXT}/Dockerfile" ]]; then
    echo "ERROR: Cannot find Dockerfile at ${BUILD_CONTEXT}/Dockerfile" >&2
    exit 1
fi
echo "  Build context: ${BUILD_CONTEXT}"

# [2/4] Verify docker is available
echo "[2/4] Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not in PATH." >&2
    exit 1
fi
echo "  Docker found: $(command -v docker)"

# [3/4] Assemble the build command
echo "[3/4] Assembling build command"
CMD=(docker buildx build)
CMD+=(--load)
CMD+=(-t "${IMAGE_TAG}")

if [[ -n "${PLATFORM}" ]]; then
    CMD+=(--platform "${PLATFORM}")
fi

if [[ -n "${NO_CACHE}" ]]; then
    CMD+=(--no-cache)
fi

CMD+=("${BUILD_CONTEXT}")

echo "  Tag: ${IMAGE_TAG}"
if [[ -n "${PLATFORM}" ]]; then
    echo "  Platform: ${PLATFORM}"
fi

# [4/4] Build image
echo "[4/4] Building image"
echo "  ${CMD[*]}"
echo ""

exec "${CMD[@]}"
