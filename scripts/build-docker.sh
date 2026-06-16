#!/usr/bin/env bash
# -------------------------------------------------------------------
# build-docker.sh — Build the ShakeMap Docker image locally.
#
# Usage:
#   ./scripts/build-docker.sh [OPTIONS]
#
# Options:
#   --tag TAG           Image name:tag  (default: shakemap-service:latest)
#   --platform PLAT     Target platform (default: current docker default)
#   --no-cache          Build without layer cache
#   --help              Show this help message
#
# Examples:
#   ./scripts/build-docker.sh
#   ./scripts/build-docker.sh --tag shakemap-service:phase01
#   ./scripts/build-docker.sh --platform linux/amd64
#   ./scripts/build-docker.sh --tag shakemap-service:test --platform linux/amd64
#   ./scripts/build-docker.sh --no-cache
# -------------------------------------------------------------------
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────
IMAGE_TAG="shakemap-service:latest"
PLATFORM=""
NO_CACHE=""

# ── Parse arguments ───────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --tag requires a value (e.g. --tag shakemap-service:phase01)" >&2
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

# ── Locate the build context (repo root = parent of scripts/) ─────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_CONTEXT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ ! -f "${BUILD_CONTEXT}/Dockerfile" ]]; then
    echo "ERROR: Cannot find Dockerfile at ${BUILD_CONTEXT}/Dockerfile" >&2
    exit 1
fi

# ── Verify docker is available ────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not in PATH." >&2
    exit 1
fi

# ── Assemble the build command ────────────────────────────────────
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

# ── Print and execute ─────────────────────────────────────────────
echo ""
echo "▸ ${CMD[*]}"
echo ""

exec "${CMD[@]}"
