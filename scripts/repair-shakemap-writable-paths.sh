#!/usr/bin/env bash
# Repair only service-owned writable trees. Scientific datasets and ancestors
# are excluded. Run with the service stopped and no concurrent runtime writers.
# This shell-only helper can be invoked manually with sudo; it never invokes it.
set -euo pipefail

usage() {
    echo "Usage: $0 --runtime-root DIR"
    echo "Stop the service and all runtime writers before running this helper."
    echo "Repairs products, logs, .service and data/inputs for UID:GID 1000:1000."
    echo "Requires an existing runtime/shakemap; never repairs scientific data."
    echo "On native Linux, ownership transfer can exclude a different host UID from private state."
    echo "Arrange an appropriate operator identity/access policy before repair; this helper does not widen group/other access."
}

RUNTIME_ROOT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime-root) RUNTIME_ROOT="${2:?--runtime-root requires a directory}"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ -n "${RUNTIME_ROOT}" ]] || { usage >&2; exit 2; }
[[ "${RUNTIME_ROOT}" == /* ]] || RUNTIME_ROOT="${PWD}/${RUNTIME_ROOT}"

refuse() {
    echo "ERROR: unsafe repair layout at $1: $2. Correct the layout while stopped; sudo does not resolve unsafe paths." >&2
    exit 2
}

# Reject links before canonicalization, including links hidden in ancestors.
# Trusted, idle runtime paths are a precondition: shell tools cannot prevent a
# concurrent privileged writer from exchanging a directory during this pass.
check_ancestors() {
    local current="$1"
    while [[ "${current}" != / && -n "${current}" ]]; do
        [[ ! -L "${current}" ]] || refuse "${current}" "symbolic link"
        current="$(dirname "${current}")"
    done
}
check_ancestors "${RUNTIME_ROOT}/shakemap/data"
[[ -d "${RUNTIME_ROOT}/shakemap" ]] || refuse "${RUNTIME_ROOT}/shakemap" "required directory is absent"
RUNTIME_ROOT="$(cd "${RUNTIME_ROOT}" && pwd -P)"
SERVICE_ROOT="${RUNTIME_ROOT}/shakemap"

# GNU and BSD stat expose the same facts through different format switches.
metadata() {
    local raw mode owner links identity
    if stat -c '%a %u:%g %h %d:%i' "$1" 2>/dev/null; then return; fi
    raw="$(stat -f '%p %u:%g %l %d:%i' "$1")" || return 1
    read -r mode owner links identity <<< "${raw}"
    # BSD's low protection field omits special bits; mask the full mode.
    printf '%o %s %s %s\n' "$((8#${mode} & 07777))" "${owner}" "${links}" "${identity}"
}
failure() {
    echo "ERROR: $1 failed for $2 (mode/owner/links/identity: $(metadata "$2" 2>/dev/null || echo unavailable)); service UID:GID 1000:1000." >&2
    # Shell utility status does not identify errno. Keep the utility's original
    # stderr and require an operator diagnosis before suggesting escalation.
    echo "The cause of this utility failure is not classified. Inspect the original error above; storage, mount, tool, or layout failures require their own remedy." >&2
    echo "Only if insufficient authorization is confirmed, with the service stopped, run manually:" >&2
    printf 'sudo %q --runtime-root %q\n' "$0" "${RUNTIME_ROOT}" >&2
    echo "Then rerun finalization normally. Partial repairs may have completed; rerunning this helper is safe after correcting the error." >&2
}

listing="$(mktemp "${TMPDIR:-/tmp}/shakemap-writable-paths.XXXXXX")"
trap 'rm -f -- "${listing}"' EXIT
paths=() modes=() identities=() missing=()
for relative in products logs .service data/inputs; do
    root="${SERVICE_ROOT}/${relative}"
    check_ancestors "${root}"
    if [[ ! -e "${root}" ]]; then
        missing+=("${root}")
        continue
    fi
    [[ -d "${root}" ]] || refuse "${root}" "writable root is not a directory"
    # Materialize traversal before any mutation, so unreadable descendants or
    # unsupported entries fail the entire preflight rather than half a repair.
    if ! find "${root}" -print0 > "${listing}"; then
        failure "preflight traversal" "${root}"
        exit 1
    fi
    while IFS= read -r -d '' path; do
        [[ ! -L "${path}" ]] || refuse "${path}" "symbolic link"
        [[ -d "${path}" || -f "${path}" ]] || refuse "${path}" "special filesystem entry"
        if ! details="$(metadata "${path}")"; then failure "stat" "${path}"; exit 1; fi
        read -r mode owner links identity <<< "${details}"
        [[ ! -f "${path}" || "${links}" == 1 ]] || refuse "${path}" "hard-linked file"
        paths+=("${path}"); modes+=("${mode}"); identities+=("${identity}")
    done < "${listing}"
done

# All selected existing trees passed preflight. Create only selected roots;
# missing ancestors belong to the operator and are never prepared here.
if [[ "${SUDO_UID:-$(id -u)}" != 1000 ]]; then
    echo "WARNING: on native Linux, transferring private state to UID 1000 can remove this operator's host access and prevent later host finalization. Arrange an appropriate operator identity/access policy; this helper does not widen group/other access." >&2
fi
for path in ${missing[@]+"${missing[@]}"}; do
    if ! mkdir "${path}"; then failure "mkdir" "${path}"; exit 1; fi
    details="$(metadata "${path}")"
    read -r mode owner links identity <<< "${details}"
    paths+=("${path}"); modes+=("${mode}"); identities+=("${identity}")
done
for index in ${paths[@]+"${!paths[@]}"}; do
    path="${paths[index]}"
    check_ancestors "${path}"
    details="$(metadata "${path}")"
    read -r mode owner links identity <<< "${details}"
    [[ "${identity}" == "${identities[index]}" ]] || refuse "${path}" "entry changed after preflight"
    [[ ! -f "${path}" || "${links}" == 1 ]] || refuse "${path}" "file became hard-linked"
    original="${modes[index]}"
    # chown can clear set-ID bits even on partial failure. Restore the complete
    # original mode plus required owner access immediately, including on error.
    required=600
    [[ ! -d "${path}" ]] || required=700
    printf -v repaired '%o' "$((8#${original} | 8#${required}))"
    result=0
    if [[ "${owner}" != 1000:1000 ]]; then
        chown -h 1000:1000 "${path}" || result=$?
    fi
    if [[ "${result}" != 0 ]]; then
        failure "chown -h 1000:1000" "${path}"
        chmod "${original}" "${path}" || failure "restore mode ${original}" "${path}"
        exit "${result}"
    fi
    if chmod "${repaired}" "${path}"; then :; else
        result=$?
        failure "chmod ${repaired}" "${path}"
        chmod "${original}" "${path}" || failure "restore mode ${original}" "${path}"
        exit "${result}"
    fi
    details="$(metadata "${path}")"
    read -r mode owner links identity <<< "${details}"
    if [[ "$((8#${mode}))" != "$((8#${repaired}))" ]]; then
        failure "verify preserved mode ${repaired}" "${path}"
        exit 1
    fi
done
echo "Service writable paths repaired for UID:GID 1000:1000; rerun finalization normally."
