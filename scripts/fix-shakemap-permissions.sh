#!/usr/bin/env bash
set -uo pipefail

RUNTIME_ROOT="${RUNTIME_ROOT:-./runtime}"
TARGET=""
TARGET_SEEN=0

usage() {
    cat <<EOF
Usage: $0 [--runtime-root DIR] [--target DATA_RELATIVE_PATH]

Without --target, the existing global, regional, and test data folders are repaired.
An explicit target is one file or directory beneath those folders, relative to
<RUNTIME_ROOT>/shakemap/data.

Examples:
  $0 --runtime-root ./runtime
  $0 --runtime-root ./runtime --target 'regional/alps/config file.conf'
  $0 --runtime-root /srv/shakemap-runtime --target global/vs30
EOF
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
        --target)
            [[ "$TARGET_SEEN" -eq 0 ]] || {
                echo "--target may be specified only once; run the command again for another path." >&2
                exit 2
            }
            [[ $# -ge 2 && -n "$2" ]] || {
                echo "--target requires one path relative to <RUNTIME_ROOT>/shakemap/data" >&2
                exit 2
            }
            TARGET="$2"
            TARGET_SEEN=1
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
# Prefix bare relative roots so commands cannot parse them as options.
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

preflight_failed=0

require_real_directory() {
    local path="$1"
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
}

for path in "$RUNTIME_ROOT" "$SERVICE_ROOT" "$DATA_ROOT"; do
    require_real_directory "$path"
done
[[ "$preflight_failed" -eq 0 ]] || exit 1

target_components=()
validate_target() {
    local remainder="$TARGET"
    local component

    case "$TARGET" in
        /*)
            printf 'Target must be relative to %q, not an absolute path: %q\n' "$DATA_ROOT" "$TARGET" >&2
            return 1
            ;;
        */|*//*)
            printf 'Target contains an empty path component: %q\n' "$TARGET" >&2
            return 1
            ;;
    esac

    while :; do
        if [[ "$remainder" == */* ]]; then
            component="${remainder%%/*}"
            remainder="${remainder#*/}"
        else
            component="$remainder"
            remainder=""
        fi
        if [[ -z "$component" || "$component" == "." || "$component" == ".." ]]; then
            printf 'Target contains an unsafe path component: %q\n' "$TARGET" >&2
            return 1
        fi
        target_components+=("$component")
        [[ -n "$remainder" ]] || break
    done

    case "${target_components[0]}" in
        global|regional|test) ;;
        *)
            printf 'Target must begin with global, regional, or test: %q\n' "$TARGET" >&2
            return 1
            ;;
    esac
}

selected_directories=()
selected_files=()

shopt -s dotglob nullglob

# Record the complete tree before changing modes so unsafe entries cannot cause a
# partly repaired result merely because they were encountered late in traversal.
preflight_tree() {
    local directory="$1"
    local entry
    local kind

    if [[ -L "$directory" ]]; then
        printf 'Unsafe symbolic link in selected tree: %q. Replace it with a real directory before rerunning.\n' "$directory" >&2
        preflight_failed=1
        return
    fi
    if [[ ! -d "$directory" ]]; then
        printf 'Selected directory is missing or inaccessible: %q. Check the path and its traversal permissions.\n' "$directory" >&2
        preflight_failed=1
        return
    fi
    if [[ ! -r "$directory" || ! -x "$directory" ]] \
        || ! ls -A "$directory" >/dev/null 2>&1
    then
        printf 'Could not inspect directory %q (%s). Grant the invoking user read and traversal access before rerunning.\n' \
            "$directory" "$(metadata "$directory")" >&2
        preflight_failed=1
        return
    fi

    selected_directories+=("$directory")
    for entry in "$directory"/*; do
        if [[ -L "$entry" ]]; then
            printf 'Unsafe symbolic link in selected tree: %q. Replace it with real content before rerunning.\n' "$entry" >&2
            preflight_failed=1
        elif [[ -d "$entry" ]]; then
            preflight_tree "$entry"
        elif [[ -f "$entry" ]]; then
            selected_files+=("$entry")
        else
            kind="$(entry_type "$entry")"
            printf 'Unsafe %s in selected tree: %q (%s). Remove it or select a narrower real file or directory.\n' \
                "$kind" "$entry" "$(metadata "$entry")" >&2
            preflight_failed=1
        fi
    done
}

ancestor_directories=("$RUNTIME_ROOT" "$SERVICE_ROOT" "$DATA_ROOT")

if [[ "$TARGET_SEEN" -eq 1 ]]; then
    validate_target || exit 2

    current="$DATA_ROOT"
    last_component=$((${#target_components[@]} - 1))
    for ((index = 0; index <= last_component; index++)); do
        current="${current}/${target_components[$index]}"
        if [[ -L "$current" ]]; then
            printf 'Unsafe symbolic link in target ancestry: %q. Select a path containing only real directories and a real file or directory.\n' "$current" >&2
            preflight_failed=1
            break
        elif [[ ! -e "$current" ]]; then
            printf 'Selected target is missing or inaccessible at %q. Check the path and its traversal permissions.\n' "$current" >&2
            preflight_failed=1
            break
        elif [[ "$index" -lt "$last_component" ]]; then
            if [[ ! -d "$current" ]]; then
                printf 'Target ancestry is not a directory at %q. Select an existing real file or directory.\n' "$current" >&2
                preflight_failed=1
                break
            fi
            ancestor_directories+=("$current")
        elif [[ -d "$current" ]]; then
            preflight_tree "$current"
        elif [[ -f "$current" ]]; then
            selected_files+=("$current")
        else
            printf 'Selected target is a %s, not a real regular file or directory: %q\n' \
                "$(entry_type "$current")" "$current" >&2
            preflight_failed=1
        fi
    done
else
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
    if [[ "${#present_roots[@]}" -eq 0 ]]; then
        echo "None of the default data folders exists; expected at least one of global, regional, or test." >&2
        preflight_failed=1
    fi
    if [[ "$preflight_failed" -eq 0 ]]; then
        for path in "${present_roots[@]}"; do
            preflight_tree "$path"
        done
    fi
fi

[[ "$preflight_failed" -eq 0 ]] || exit 1

status=0

repair_mode() {
    local mode="$1"
    local path="$2"
    local expected_type="$3"
    local details
    local had_setuid=0
    local had_setgid=0
    local had_sticky=0
    local chmod_failed=0

    # Recheck the recorded type immediately before chmod so a replaced entry is
    # never followed or treated as the object that passed preflight.
    if [[ -L "$path" ]] \
        || [[ "$expected_type" == "directory" && ! -d "$path" ]] \
        || [[ "$expected_type" == "file" && ! -f "$path" ]]
    then
        printf 'Path changed after permission preflight: %q. Rerun after the data tree is stable.\n' "$path" >&2
        status=1
        return
    fi
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

for path in "${ancestor_directories[@]}"; do
    repair_mode a+x "$path" directory
done
if [[ "${#selected_directories[@]}" -gt 0 ]]; then
    for path in "${selected_directories[@]}"; do
        repair_mode a+rx "$path" directory
    done
fi
if [[ "${#selected_files[@]}" -gt 0 ]]; then
    for path in "${selected_files[@]}"; do
        repair_mode a+r "$path" file
    done
fi

[[ "$status" -eq 0 ]] || exit 1

if [[ "$TARGET_SEEN" -eq 1 ]]; then
    printf 'Permissions repaired for %q beneath %s.\n' "$TARGET" "$(quote_path "$DATA_ROOT")"
else
    echo "Permissions repaired in the existing default data folders under $(quote_path "$DATA_ROOT")."
fi
