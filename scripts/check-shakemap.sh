#!/usr/bin/env bash
# Query the running service's read-only diagnostic endpoint. No Docker setup,
# readiness changes, profile materialization, or native calculations occur.
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: $0 [--configuration NAME] [--url URL]"
    echo "Static profile diagnostics only. Exit 0: no known blockers; 1: blocked; 2: incomplete."
    exit 0
fi

configuration=global
service_url=http://localhost:9010
while [[ $# -gt 0 ]]; do
    case "$1" in
        --configuration) configuration="${2:?--configuration requires a name}"; shift 2 ;;
        --url) service_url="${2:?--url requires a URL}"; shift 2 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "${VIRTUAL_ENV:-}" || ! -x "${VIRTUAL_ENV}/bin/shake-in-docker" ]]; then
    echo "ERROR: activate the project Python environment with shake-in-docker installed." >&2
    exit 2
fi
exec "${VIRTUAL_ENV}/bin/shake-in-docker" --url "${service_url}" check --configuration "${configuration}"
