#!/usr/bin/env bash
# Stop the canonical service through Docker's 60-second shutdown boundary.
set -euo pipefail

if [[ $# -gt 0 ]]; then
    [[ "$1" == "--help" || "$1" == "-h" ]] && { echo "Usage: $0"; exit 0; }
    echo "ERROR: this helper takes no options" >&2
    exit 2
fi

CONTAINER="shakemap-docker"
containers="$(docker container ls --all --filter "name=^/${CONTAINER}$" --format '{{.Names}}')"
if [[ -z "${containers}" ]]; then
    exit 0
fi
if [[ "${containers}" != "${CONTAINER}" ]]; then
    echo "ERROR: Docker returned an unexpected canonical-container listing." >&2
    exit 1
fi
running="$(docker container inspect --format '{{.State.Running}}' "${CONTAINER}")"
case "${running}" in
    false) exit 0 ;;
    true)
        docker stop --time 65 "${CONTAINER}" >/dev/null
        echo "Canonical ShakeMap service is stopped."
        ;;
    *)
        echo "ERROR: canonical container has an unrecognized running state." >&2
        exit 1
        ;;
esac
