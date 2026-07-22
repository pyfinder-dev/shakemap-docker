#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# start-shakemap-docker.sh -- Start the ShakeMap service container.
#
# Starts a Docker container with sensible defaults.  Override supported
# settings via command-line arguments.
#
# Usage:
#   ./scripts/start-shakemap-docker.sh [OPTIONS]
#
# Options:
#   --name NAME         Container name       (default: shakemap-docker)
#   --runtime DIR       Host runtime dir     (default: ./runtime)
#   --port PORT         Host port mapping     (default: 9010)
#   --image IMAGE       Image name:tag        (default: shakemap-docker:latest)
#   --env KEY=VALUE     Extra env var (repeatable)
#   --detach            Run in background     (default: yes)
#   --foreground        Run in foreground (replaces --detach)
#   --help              Show this help message
#
# Examples:
#   ./scripts/start-shakemap-docker.sh
#   ./scripts/start-shakemap-docker.sh --name shakemap-docker-test --port 8080
#   ./scripts/start-shakemap-docker.sh --runtime /data/shakemap --image shakemap-docker:test
#   ./scripts/start-shakemap-docker.sh --env SHAKEMAP_SKIP_DATA_DOWNLOAD=1
# ------------------------------------------------------------------

# -- Defaults --
CONTAINER_NAME="shakemap-docker"
RUNTIME_DIR="./runtime"
HOST_PORT="9010"
IMAGE_TAG="shakemap-docker:latest"
DETACH="yes"
EXTRA_ENVS=()
RESERVED_IDENTITY_KEYS=(
    "SHAKEMAP_IMAGE_ID"
    "SHAKEMAP_IMAGE_DIGEST"
    "SHAKEMAP_BUILD_IDENTITY_FILE"
)

validate_extra_env() {
    local candidate="$1"
    local reserved
    for reserved in "${RESERVED_IDENTITY_KEYS[@]}"; do
        if [[ "${candidate}" =~ ^${reserved}([^A-Za-z0-9_]|$) ]]; then
            echo "ERROR: --env cannot set reserved identity key ${reserved}; remove that override." >&2
            return 1
        fi
    done
    if [[ ! "${candidate}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
        echo "ERROR: --env requires a valid KEY=VALUE assignment: ${candidate}" >&2
        return 1
    fi
}

# -- Parse arguments --
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --name requires a value" >&2; exit 1
            fi
            CONTAINER_NAME="$2"; shift 2
            ;;
        --runtime)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --runtime requires a value" >&2; exit 1
            fi
            RUNTIME_DIR="$2"; shift 2
            ;;
        --port)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --port requires a value" >&2; exit 1
            fi
            HOST_PORT="$2"; shift 2
            ;;
        --image)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --image requires a value" >&2; exit 1
            fi
            IMAGE_TAG="$2"; shift 2
            ;;
        --env)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --env requires a KEY=VALUE" >&2; exit 1
            fi
            validate_extra_env "$2" || exit 1
            EXTRA_ENVS+=("$2"); shift 2
            ;;
        --detach)
            DETACH="yes"; shift
            ;;
        --foreground)
            DETACH="no"; shift
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

# [1/4] Validate Docker is available
echo "[start] [1/4] Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not in PATH." >&2
    exit 1
fi

# Deployment identity is supplied by Docker at startup. It is intentionally
# separate from the immutable build manifest, and a registry digest remains
# unavailable when the locally loaded image has no RepoDigest.
DEPLOYMENT_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE_TAG}" 2>/dev/null || true)"
DEPLOYMENT_IMAGE_DIGESTS="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${IMAGE_TAG}" 2>/dev/null || true)"
DEPLOYMENT_IMAGE_DIGEST="${DEPLOYMENT_IMAGE_DIGESTS%%$'\n'*}"

# [2/4] Ensure runtime directory exists
echo "[start] [2/4] Ensuring runtime directory exists"
RUNTIME_ABS="$(cd "$(dirname "${RUNTIME_DIR}")" 2>/dev/null && pwd)/$(basename "${RUNTIME_DIR}")" || RUNTIME_ABS="${RUNTIME_DIR}"
mkdir -p "${RUNTIME_DIR}"
echo "  Runtime dir: ${RUNTIME_ABS}"

# [3/4] Check for existing container
echo "[start] [3/4] Checking for existing container"
if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
    STATE="$(docker container inspect -f '{{.State.Running}}' "${CONTAINER_NAME}")"
    if [ "${STATE}" = "true" ]; then
        echo "ERROR: Container '${CONTAINER_NAME}' is already running; it was not modified." >&2
        echo "Stop it explicitly with: docker stop ${CONTAINER_NAME}" >&2
        exit 1
    else
        echo "ERROR: Container '${CONTAINER_NAME}' already exists but is stopped; it was not modified." >&2
        echo "Resume it with: docker start ${CONTAINER_NAME}" >&2
        echo "To recreate it, first remove it explicitly with: docker rm ${CONTAINER_NAME}" >&2
        exit 1
    fi
fi

# [4/4] Start container
echo "[start] [4/4] Starting container"

CMD=(docker run)

if [ "${DETACH}" = "yes" ]; then
    CMD+=(-d)
fi

CMD+=(--name "${CONTAINER_NAME}")
CMD+=(-p "${HOST_PORT}:9010")
CMD+=(-v "${RUNTIME_ABS}:/home/sysop/runtime")

if [[ -n "${DEPLOYMENT_IMAGE_ID}" ]]; then
    CMD+=(-e "SHAKEMAP_IMAGE_ID=${DEPLOYMENT_IMAGE_ID}")
fi
if [[ -n "${DEPLOYMENT_IMAGE_DIGEST}" ]]; then
    CMD+=(-e "SHAKEMAP_IMAGE_DIGEST=${DEPLOYMENT_IMAGE_DIGEST}")
fi

for env_var in "${EXTRA_ENVS[@]+"${EXTRA_ENVS[@]}"}"; do
    CMD+=(-e "${env_var}")
done

CMD+=("${IMAGE_TAG}")

echo "  Name:     ${CONTAINER_NAME}"
echo "  Port:     ${HOST_PORT}:9010"
echo "  Runtime:  ${RUNTIME_ABS}:/home/sysop/runtime"
echo "  Image:    ${IMAGE_TAG}"
echo "  Image ID: ${DEPLOYMENT_IMAGE_ID:-unavailable}"
echo "  Digest:   ${DEPLOYMENT_IMAGE_DIGEST:-unavailable}"
echo "  Mode:     $([ "${DETACH}" = "yes" ] && echo "detached" || echo "foreground")"
if [ ${#EXTRA_ENVS[@]} -gt 0 ]; then
    echo "  Env:      ${EXTRA_ENVS[*]}"
fi
echo ""
echo "  ${CMD[*]}"
echo ""

"${CMD[@]}"

if [ "${DETACH}" = "yes" ]; then
    echo ""
    echo "Container '${CONTAINER_NAME}' started."
    echo ""
    echo "Next steps:"
    echo "  1. Check configuration: curl -fsS http://localhost:${HOST_PORT}/config"
    echo "  2. Check readiness:     curl -fsS http://localhost:${HOST_PORT}/healthz"
    echo "  Scientific datasets remain external; not_ready can be the correct state."
fi
