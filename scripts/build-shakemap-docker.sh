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
#   --release-tag TAG   Final stable upstream tag override (requires --release-commit)
#   --release-commit ID Full upstream commit override (requires --release-tag)
#   --help              Show this help message
#
# Examples:
#   ./scripts/build-shakemap-docker.sh
#   ./scripts/build-shakemap-docker.sh --tag shakemap-docker:test
#   ./scripts/build-shakemap-docker.sh --platform linux/amd64
#   ./scripts/build-shakemap-docker.sh --no-cache
# -------------------------------------------------------------------
set -euo pipefail

# -- Defaults --
IMAGE_TAG="shakemap-docker:latest"
PLATFORM=""
NO_CACHE=""
RELEASE_TAG_OVERRIDE=""
RELEASE_COMMIT_OVERRIDE=""

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
        --release-commit)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --release-commit requires a full commit" >&2
                exit 1
            fi
            RELEASE_COMMIT_OVERRIDE="$2"
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

if [[ -n "${RELEASE_TAG_OVERRIDE}" && -z "${RELEASE_COMMIT_OVERRIDE}" ]] || \
   [[ -z "${RELEASE_TAG_OVERRIDE}" && -n "${RELEASE_COMMIT_OVERRIDE}" ]]; then
    echo "ERROR: --release-tag and --release-commit must be supplied together." >&2
    exit 1
fi

# [1/5] Locate the build context (repo root = parent of scripts/)
echo "[1/5] Locating Dockerfile"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_CONTEXT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ ! -f "${BUILD_CONTEXT}/Dockerfile" ]]; then
    echo "ERROR: Cannot find Dockerfile at ${BUILD_CONTEXT}/Dockerfile" >&2
    exit 1
fi
echo "  Build context: ${BUILD_CONTEXT}"

# [2/5] Resolve the official stable release once, before Docker starts.
echo "[2/5] Resolving latest official stable USGS ShakeMap release"
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
    RESOLVE_ARGS+=(--release-commit "${RELEASE_COMMIT_OVERRIDE}")
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

echo "  Release tag: ${SHAKEMAP_RELEASE_TAG}"
echo "  Source commit: ${SHAKEMAP_SOURCE_COMMIT}"
echo "  Source URL: ${SHAKEMAP_SOURCE_URL}"

# [3/5] Verify docker is available
echo "[3/5] Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not in PATH." >&2
    exit 1
fi
echo "  Docker found: $(command -v docker)"

# Record service-source facts available at invocation without embedding Git.
SERVICE_SOURCE_COMMIT="unavailable"
SERVICE_WORKTREE_DIRTY="unknown"
if git -C "${BUILD_CONTEXT}" rev-parse --verify HEAD >/dev/null 2>&1; then
    SERVICE_SOURCE_COMMIT="$(git -C "${BUILD_CONTEXT}" rev-parse HEAD)"
    if git -C "${BUILD_CONTEXT}" diff --quiet --ignore-submodules -- && \
       git -C "${BUILD_CONTEXT}" diff --cached --quiet --ignore-submodules -- && \
       [[ -z "$(git -C "${BUILD_CONTEXT}" ls-files --others --exclude-standard)" ]]; then
        SERVICE_WORKTREE_DIRTY="false"
    else
        SERVICE_WORKTREE_DIRTY="true"
    fi
fi
BUILD_TIMESTAMP_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# [4/5] Assemble the build command through the same tested constructor.
echo "[4/5] Assembling immutable build command"
COMMAND_ARGS=(
    -m shakemap_service.release build-command
    --image-tag "${IMAGE_TAG}"
    --build-context "${BUILD_CONTEXT}"
    --release-tag "${SHAKEMAP_RELEASE_TAG}"
    --release-commit "${SHAKEMAP_SOURCE_COMMIT}"
    --repository-url "${SHAKEMAP_SOURCE_URL}"
    --service-commit "${SERVICE_SOURCE_COMMIT}"
    --service-worktree-dirty "${SERVICE_WORKTREE_DIRTY}"
    --build-timestamp-utc "${BUILD_TIMESTAMP_UTC}"
)
if [[ -n "${PLATFORM}" ]]; then
    COMMAND_ARGS+=(--platform "${PLATFORM}")
fi
if [[ -n "${NO_CACHE}" ]]; then
    COMMAND_ARGS+=(--no-cache)
fi

COMMAND_OUTPUT="$(cd "${BUILD_CONTEXT}" && "${PYTHON_BIN}" "${COMMAND_ARGS[@]}")"
CMD=()
while IFS= read -r line; do
    CMD+=("${line}")
done <<< "${COMMAND_OUTPUT}"
if [[ "${#CMD[@]}" -lt 2 ]]; then
    echo "ERROR: Build command constructor returned no command." >&2
    exit 1
fi

echo "  Tag: ${IMAGE_TAG}"
if [[ -n "${PLATFORM}" ]]; then
    echo "  Platform: ${PLATFORM}"
fi
echo "  Service commit: ${SERVICE_SOURCE_COMMIT}"
echo "  Service worktree dirty: ${SERVICE_WORKTREE_DIRTY}"
echo "  Build timestamp: ${BUILD_TIMESTAMP_UTC}"

# [5/5] Build image
echo "[5/5] Building image"
echo "  ${CMD[*]}"
echo ""

exec "${CMD[@]}"
