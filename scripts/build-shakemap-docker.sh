#!/usr/bin/env bash
# Build and verify an untagged image before promoting the canonical image tag.
set -euo pipefail

PLATFORM=""
NO_CACHE=""

usage() {
    cat <<'EOF'
Usage: ./scripts/build-shakemap-docker.sh [OPTIONS]

Build the release declared in VERSIONS.env, verify the untagged candidate by
image identity, and then assign shakemap-docker:latest.

Options:
  --platform PLAT  Target platform (default: current Docker default)
  --no-cache       Build without layer cache
  --help           Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --platform requires a value" >&2
                exit 2
            fi
            PLATFORM="$2"
            shift 2
            ;;
        --no-cache)
            NO_CACHE="yes"
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_CONTEXT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
VERSIONS_FILE="${BUILD_CONTEXT}/VERSIONS.env"
CANONICAL_IMAGE="shakemap-docker:latest"

if [[ ! -f "${BUILD_CONTEXT}/Dockerfile" || ! -f "${VERSIONS_FILE}" ]]; then
    echo "ERROR: Dockerfile or VERSIONS.env is missing from ${BUILD_CONTEXT}" >&2
    exit 1
fi

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    echo "ERROR: an activated project virtual environment is required." >&2
    echo "Activate the project environment, then install this project with:" >&2
    echo "  python -m pip install -e ." >&2
    exit 1
fi
PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
if [[ ! -x "${PYTHON_BIN}" || "$(command -v python 2>/dev/null || true)" != "${PYTHON_BIN}" ]]; then
    echo "ERROR: the active python command does not belong to VIRTUAL_ENV=${VIRTUAL_ENV}." >&2
    echo "Activate the project environment, then install this project with:" >&2
    echo "  python -m pip install -e ." >&2
    exit 1
fi
CONSOLE_PATH="$(command -v shake-in-docker 2>/dev/null || true)"
if [[ -z "${CONSOLE_PATH}" || "${CONSOLE_PATH}" != "${VIRTUAL_ENV}/bin/shake-in-docker" ]]; then
    echo "ERROR: shake-in-docker is not installed in the active project environment." >&2
    echo "From ${BUILD_CONTEXT}, install it with:" >&2
    echo "  python -m pip install -e ." >&2
    exit 1
fi
if ! "${PYTHON_BIN}" -c 'import importlib.metadata as m; d=m.distribution("shakemap-docker-service"); raise SystemExit(0 if any(ep.group == "console_scripts" and ep.name == "shake-in-docker" and ep.value == "shakemap_service.cli:main" for ep in d.entry_points) else 1)'; then
    echo "ERROR: the active project installation has no valid shake-in-docker entry point." >&2
    echo "Reinstall it from ${BUILD_CONTEXT} with:" >&2
    echo "  python -m pip install -e ." >&2
    exit 1
fi

RESOLUTION_OUTPUT="$(
    cd "${BUILD_CONTEXT}"
    "${PYTHON_BIN}" -m shakemap_service.release resolve \
        --versions-file "${VERSIONS_FILE}"
)"
RESOLUTION_LINES=()
while IFS= read -r line; do
    RESOLUTION_LINES+=("${line}")
done <<< "${RESOLUTION_OUTPUT}"
if [[ "${#RESOLUTION_LINES[@]}" -ne 3 ]]; then
    echo "ERROR: release resolver returned malformed output" >&2
    exit 1
fi
SHAKEMAP_RELEASE_TAG="${RESOLUTION_LINES[0]}"
SHAKEMAP_SOURCE_COMMIT="${RESOLUTION_LINES[1]}"
SHAKEMAP_SOURCE_URL="${RESOLUTION_LINES[2]}"
SHAKEMAP_RELEASE_VERSION="${SHAKEMAP_RELEASE_TAG#v}"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not in PATH." >&2
    exit 1
fi

PRECEDING_IMAGE_ID="$(
    docker image inspect --format '{{.Id}}' "${CANONICAL_IMAGE}" 2>/dev/null || true
)"
CANDIDATE_ID=""
PROMOTED="no"
cleanup_candidate() {
    result=$?
    if [[ "${PROMOTED}" != "yes" && -n "${CANDIDATE_ID}" && "${CANDIDATE_ID}" != "${PRECEDING_IMAGE_ID}" ]]; then
        tags="$(docker image inspect --format '{{json .RepoTags}}' "${CANDIDATE_ID}" 2>/dev/null || true)"
        if [[ "${tags}" == "null" || "${tags}" == "[]" || -z "${tags}" ]]; then
            docker image rm "${CANDIDATE_ID}" >/dev/null 2>&1 || true
        fi
    fi
    exit "${result}"
}
trap cleanup_candidate EXIT

BUILD_TIMESTAMP_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
BUILD_COMMAND=(docker build --quiet)
if [[ -n "${PLATFORM}" ]]; then
    BUILD_COMMAND+=(--platform "${PLATFORM}")
fi
if [[ -n "${NO_CACHE}" ]]; then
    BUILD_COMMAND+=(--no-cache)
fi
BUILD_COMMAND+=(
    --build-arg "SHAKEMAP_SOURCE_URL=${SHAKEMAP_SOURCE_URL}"
    --build-arg "SHAKEMAP_RELEASE_TAG=${SHAKEMAP_RELEASE_TAG}"
    --build-arg "SHAKEMAP_RELEASE_VERSION=${SHAKEMAP_RELEASE_VERSION}"
    --build-arg "SHAKEMAP_SOURCE_COMMIT=${SHAKEMAP_SOURCE_COMMIT}"
    --build-arg "BUILD_TIMESTAMP_UTC=${BUILD_TIMESTAMP_UTC}"
    "${BUILD_CONTEXT}"
)

echo "Building declared ShakeMap release ${SHAKEMAP_RELEASE_TAG} as an untagged candidate"
CANDIDATE_ID="$("${BUILD_COMMAND[@]}")"
if [[ ! "${CANDIDATE_ID}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "ERROR: docker build did not return one immutable candidate image identity" >&2
    exit 1
fi
INSPECTED_CANDIDATE_ID="$(docker image inspect --format '{{.Id}}' "${CANDIDATE_ID}")"
if [[ "${INSPECTED_CANDIDATE_ID}" != "${CANDIDATE_ID}" ]]; then
    echo "ERROR: built candidate identity could not be verified" >&2
    exit 1
fi

echo "Verifying candidate ${CANDIDATE_ID} in an anonymous container"
docker run --rm --network none \
    --entrypoint /app/scripts/verify-shakemap-image.sh \
    "${CANDIDATE_ID}"

docker image tag "${CANDIDATE_ID}" "${CANONICAL_IMAGE}"
PROMOTED_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${CANONICAL_IMAGE}")"
if [[ "${PROMOTED_IMAGE_ID}" != "${CANDIDATE_ID}" ]]; then
    echo "ERROR: canonical image tag does not identify the verified candidate" >&2
    exit 1
fi
PROMOTED="yes"
trap - EXIT
echo "Promoted verified ${SHAKEMAP_RELEASE_TAG} image to ${CANONICAL_IMAGE}"
echo "This image-level result does not establish deployment or calculation readiness."
