#!/usr/bin/env bash
set -uo pipefail

RUNTIME_ROOT="${RUNTIME_ROOT:-./runtime}"

usage() {
    echo "Usage: $0 [--runtime-root DIR]"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime-root)
            [[ $# -ge 2 && -n "$2" ]] || {
                echo "--runtime-root requires a directory" >&2
                exit 2
            }
            RUNTIME_ROOT="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option or positional path: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

while [[ "$RUNTIME_ROOT" != "/" && "$RUNTIME_ROOT" == */ ]]; do
    RUNTIME_ROOT="${RUNTIME_ROOT%/}"
done
# Bare relative roots are prefixed so permission commands cannot parse them as options.
case "$RUNTIME_ROOT" in
    /*|./*|../*) ;;
    *) RUNTIME_ROOT="./${RUNTIME_ROOT}" ;;
esac

SERVICE_ROOT="${RUNTIME_ROOT}/shakemap"
DATA_ROOT="${SERVICE_ROOT}/data"
GLOBAL_ROOT="${DATA_ROOT}/global"
REGIONAL_ROOT="${DATA_ROOT}/regional"
TEST_ROOT="${DATA_ROOT}/test"

quote_path() {
    printf '%q' "$1"
}

metadata() {
    stat -f '%Sp %u:%g' "$1" 2>/dev/null \
        || stat -c '%A %u:%g' "$1" 2>/dev/null \
        || printf 'mode/owner unavailable'
}

preflight_failed=0
for path in \
    "$RUNTIME_ROOT" \
    "$SERVICE_ROOT" \
    "$DATA_ROOT"
do
    if [[ -L "$path" ]]; then
        printf 'Unsafe symbolic-link directory: %q. Replace it with a real directory before rerunning.\n' "$path" >&2
        preflight_failed=1
    elif [[ ! -e "$path" ]]; then
        printf 'Required directory is missing or inaccessible: %q. Correct RUNTIME_ROOT or create the canonical runtime layout first.\n' "$path" >&2
        preflight_failed=1
    elif [[ ! -d "$path" ]]; then
        printf 'Required path is not a directory: %q. Move it aside and create the canonical directory before rerunning.\n' "$path" >&2
        preflight_failed=1
    fi
done
[[ "$preflight_failed" -eq 0 ]] || exit 1

present_roots=()
for path in "$GLOBAL_ROOT" "$REGIONAL_ROOT" "$TEST_ROOT"; do
    if [[ -L "$path" ]]; then
        printf 'Unsafe symbolic-link data folder: %q. Replace it with a real directory before rerunning.\n' "$path" >&2
        preflight_failed=1
    elif [[ ! -e "$path" ]]; then
        printf 'SKIP missing data folder: %q\n' "$path"
    elif [[ ! -d "$path" ]]; then
        printf 'Data path is not a directory: %q. Move it aside or replace it with a real directory before rerunning.\n' "$path" >&2
        preflight_failed=1
    else
        present_roots+=("$path")
    fi
done
[[ "$preflight_failed" -eq 0 ]] || exit 1
if [[ "${#present_roots[@]}" -eq 0 ]]; then
    echo "None of the default data folders exists; expected at least one of global, regional, or test." >&2
    exit 1
fi

status=0

repair_mode() {
    local mode="$1"
    local path="$2"
    local details
    local had_setuid=0
    local had_setgid=0
    local had_sticky=0
    local chmod_failed=0
    [[ -u "$path" ]] && had_setuid=1
    [[ -g "$path" ]] && had_setgid=1
    [[ -k "$path" ]] && had_sticky=1

    # Preserve special bits that some chmod implementations clear during mode changes.
    chmod "$mode" "$path" || chmod_failed=1
    if [[ "$had_setuid" -eq 1 && ! -u "$path" ]]; then
        chmod u+s "$path" || chmod_failed=1
    fi
    if [[ "$had_setgid" -eq 1 && ! -g "$path" ]]; then
        chmod g+s "$path" || chmod_failed=1
    fi
    if [[ "$had_sticky" -eq 1 && ! -k "$path" ]]; then
        chmod +t "$path" || chmod_failed=1
    fi
    [[ "$chmod_failed" -eq 0 ]] && return

    details="$(metadata "$path")"
    printf 'Could not apply chmod %s to %q (%s). Rerun as the path owner or apply that additive chmod with sufficient host authority.\n' \
        "$mode" "$path" "$details" >&2
    status=1
}

entry_type() {
    local path="$1"
    if [[ -p "$path" ]]; then
        printf 'FIFO'
    elif [[ -S "$path" ]]; then
        printf 'socket'
    elif [[ -b "$path" ]]; then
        printf 'block device'
    elif [[ -c "$path" ]]; then
        printf 'character device'
    else
        printf 'special filesystem entry'
    fi
}

repair_directory() {
    local directory="$1"
    local entry
    local kind

    repair_mode a+rx "$directory"
    if ! ls -A "$directory" >/dev/null 2>&1; then
        printf 'Could not list directory %q (%s). Rerun as the path owner or grant read and traversal permission.\n' \
            "$directory" "$(metadata "$directory")" >&2
        status=1
        return
    fi

    for entry in "$directory"/*; do
        if [[ -L "$entry" ]]; then
            printf 'Skipped symbolic link %q. Copy real content into the tree or repair its target explicitly.\n' "$entry" >&2
            status=1
        elif [[ -d "$entry" ]]; then
            repair_directory "$entry"
        elif [[ -f "$entry" ]]; then
            repair_mode a+r "$entry"
        else
            kind="$(entry_type "$entry")"
            printf 'Skipped %s %q (%s). Remove it from the scientific/configuration tree after review.\n' \
                "$kind" "$entry" "$(metadata "$entry")" >&2
            status=1
        fi
    done
}

shopt -s dotglob nullglob

# Ancestors need traversal only; listing permission begins at the fixed data trees.
for path in "$RUNTIME_ROOT" "$SERVICE_ROOT" "$DATA_ROOT"; do
    repair_mode a+x "$path"
done

for path in "${present_roots[@]}"; do
    repair_directory "$path"
done

if [[ "$status" -ne 0 ]]; then
    exit 1
fi

echo "Permissions repaired in the existing default data folders under $(quote_path "$DATA_ROOT")."
