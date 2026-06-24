#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# start-shakemap-docker.sh -- Start the ShakeMap service container.
#
# Starts a Docker container with sensible defaults.  Override any
# setting via command-line arguments.
#
# Usage:
#   ./scripts/start-shakemap-docker.sh [OPTIONS]
#
# Options:
#   --name NAME         Container name       (default: shakemap)
#   --runtime DIR       Host runtime dir     (default: ./runtime)
#   --port PORT         Host port mapping     (default: 9010)
#   --image IMAGE       Image name:tag        (default: shakemap-service:latest)
#   --env KEY=VALUE     Extra env var (repeatable)
#   --detach            Run in background     (default: yes)
#   --foreground        Run in foreground (replaces --detach)
#   --help              Show this help message
#
# Examples:
#   ./scripts/start-shakemap-docker.sh
#   ./scripts/start-shakemap-docker.sh --name myshakemap --port 8080
#   ./scripts/start-shakemap-docker.sh --runtime /data/shakemap --image shakemap-service:v2
#   ./scripts/start-shakemap-docker.sh --env SHAKEMAP_SKIP_DATA_DOWNLOAD=1
# ------------------------------------------------------------------

# -- Defaults --
CONTAINER_NAME="shakemap"
RUNTIME_DIR="./runtime"
HOST_PORT="9010"
IMAGE_TAG="shakemap-service:latest"
DETACH="yes"
EXTRA_ENVS=()

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

# [2/4] Ensure runtime directory exists
echo "[start] [2/4] Ensuring runtime directory exists"
RUNTIME_ABS="$(cd "$(dirname "${RUNTIME_DIR}")" 2>/dev/null && pwd)/$(basename "${RUNTIME_DIR}")" || RUNTIME_ABS="${RUNTIME_DIR}"
mkdir -p "${RUNTIME_DIR}"
echo "  Runtime dir: ${RUNTIME_ABS}"

# [3/4] Check for existing container
echo "[start] [3/4] Checking for existing container"
if docker inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
    STATE="$(docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || echo "false")"
    if [ "${STATE}" = "true" ]; then
        echo "  Container '${CONTAINER_NAME}' is already running."
        echo "  To restart: docker rm -f ${CONTAINER_NAME} && $0"
        exit 1
    else
        echo "  Removing stopped container '${CONTAINER_NAME}'"
        docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
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

for env_var in "${EXTRA_ENVS[@]+"${EXTRA_ENVS[@]}"}"; do
    CMD+=(-e "${env_var}")
done

CMD+=("${IMAGE_TAG}")

echo "  Name:     ${CONTAINER_NAME}"
echo "  Port:     ${HOST_PORT}:9010"
echo "  Runtime:  ${RUNTIME_ABS}:/home/sysop/runtime"
echo "  Image:    ${IMAGE_TAG}"
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
    echo "  1. Wait a few seconds for the service to initialize"
    echo "  2. Configure ShakeMap:"
    echo "     docker exec ${CONTAINER_NAME} /app/scripts/configure-shakemap.sh"
    echo "  3. Verify deployment:"
    echo "     ./scripts/verify-shakemap-deployment.sh ${CONTAINER_NAME} --expect ready"
fi
