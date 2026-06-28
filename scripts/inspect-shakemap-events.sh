#!/usr/bin/env bash
set -uo pipefail

# ------------------------------------------------------------------
# inspect-shakemap-events.sh -- ShakeMap Event and Queue Inspector
#
# CLI equivalent of the REST event discovery endpoints.
# Displays events, queue state, and product locations.
#
# Two modes:
#
#   (default)    Read events directly from filesystem
#   --rest       Query the REST API endpoints instead
#
# Options:
#   --status STATUS   Filter by event status (e.g. QUEUED, SUCCESS)
#   --event ID        Show detail for a single event
#   --queue           Show current queue state
#   --products ID     Show products for an event
#   --rest            Query REST API instead of filesystem
#   --help            Show this help message
#
# Usage:
#   docker exec <container> /app/scripts/inspect-shakemap-events.sh
#   docker exec <container> /app/scripts/inspect-shakemap-events.sh --status SUCCESS
#   docker exec <container> /app/scripts/inspect-shakemap-events.sh --event my_event_001
#   docker exec <container> /app/scripts/inspect-shakemap-events.sh --queue
#   docker exec <container> /app/scripts/inspect-shakemap-events.sh --products my_event_001
#   docker exec <container> /app/scripts/inspect-shakemap-events.sh --rest
#
# Exit code: always 0 (inspection only, never fails).
# ------------------------------------------------------------------

MODE="filesystem"
STATUS_FILTER=""
SINGLE_EVENT=""
SHOW_QUEUE=""
SHOW_PRODUCTS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rest)
            MODE="rest"; shift
            ;;
        --status)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --status requires a value" >&2; exit 1
            fi
            STATUS_FILTER="$2"; shift 2
            ;;
        --event)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --event requires an event_id" >&2; exit 1
            fi
            SINGLE_EVENT="$2"; shift 2
            ;;
        --queue)
            SHOW_QUEUE="yes"; shift
            ;;
        --products)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --products requires an event_id" >&2; exit 1
            fi
            SHOW_PRODUCTS="$2"; shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --status STATUS   Filter by event status"
            echo "  --event ID        Show detail for a single event"
            echo "  --queue           Show current queue state"
            echo "  --products ID     Show products for an event"
            echo "  --rest            Query REST API instead of filesystem"
            echo "  --help            Show this help"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown option: $1" >&2
            echo "Run with --help for usage." >&2
            exit 1
            ;;
    esac
done

# ------------------------------------------------------------------
# REST mode
# ------------------------------------------------------------------
if [ "${MODE}" = "rest" ]; then
    PORT="${SHAKEMAP_PORT:-9010}"
    python3 - "${PORT}" "${STATUS_FILTER}" "${SINGLE_EVENT}" "${SHOW_QUEUE}" "${SHOW_PRODUCTS}" <<'PYEOF'
import sys, json, urllib.request

port = sys.argv[1]
status_filter = sys.argv[2] if len(sys.argv) > 2 else ""
single_event = sys.argv[3] if len(sys.argv) > 3 else ""
show_queue = sys.argv[4] if len(sys.argv) > 4 else ""
show_products = sys.argv[5] if len(sys.argv) > 5 else ""

base = f"http://localhost:{port}"

def fetch(path):
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"ERROR: Could not reach {path}: {e}", file=sys.stderr)
        return None

if show_products:
    data = fetch(f"/events/{show_products}/products")
    if data:
        print(json.dumps(data, indent=2))
elif single_event:
    data = fetch(f"/events/{single_event}")
    if data:
        print(json.dumps(data, indent=2))
elif show_queue:
    data = fetch("/queue")
    if data:
        print(json.dumps(data, indent=2))
else:
    params = ""
    if status_filter:
        params = f"?status={status_filter}"
    data = fetch(f"/events{params}")
    if data:
        print(json.dumps(data, indent=2))
PYEOF
    exit 0
fi

# ------------------------------------------------------------------
# Filesystem mode
# ------------------------------------------------------------------

RUNTIME_ROOT="${RUNTIME_ROOT:-/home/sysop/runtime}"
SERVICE_ROOT="${SERVICE_ROOT:-${RUNTIME_ROOT}/shakemap}"
EVENTS_DIR="${SERVICE_ROOT}/events"
PRODUCTS_DIR="${SERVICE_ROOT}/products"

# -- Show products for a single event --
if [ -n "${SHOW_PRODUCTS}" ]; then
    echo "===== Products: ${SHOW_PRODUCTS} ====="
    PROD_DIR="${PRODUCTS_DIR}/${SHOW_PRODUCTS}"
    if [ -d "${PROD_DIR}" ]; then
        echo "Products directory: ${PROD_DIR}"
        echo ""
        find "${PROD_DIR}" -type f ! -name '.*' -printf "  %P  (%s bytes)\n" 2>/dev/null || \
            find "${PROD_DIR}" -type f ! -name '.*' | while read -r f; do
                SIZE=$(stat -c '%s' "$f" 2>/dev/null || stat -f '%z' "$f" 2>/dev/null || echo "?")
                REL="${f#${PROD_DIR}/}"
                echo "  ${REL}  (${SIZE} bytes)"
            done
    else
        echo "No products directory found at ${PROD_DIR}"
    fi
    echo ""
    echo "===== End Products ====="
    exit 0
fi

# -- Show a single event --
if [ -n "${SINGLE_EVENT}" ]; then
    STATUS_FILE="${EVENTS_DIR}/${SINGLE_EVENT}/.shakemap-service/requeststatus.json"
    echo "===== Event: ${SINGLE_EVENT} ====="
    if [ -f "${STATUS_FILE}" ]; then
        python3 -c "
import json, sys
with open('${STATUS_FILE}') as f:
    data = json.load(f)
print(json.dumps(data, indent=2))
" 2>/dev/null || cat "${STATUS_FILE}"
    else
        echo "  Status file not found: ${STATUS_FILE}"
    fi
    # Check products
    PROD_DIR="${PRODUCTS_DIR}/${SINGLE_EVENT}"
    if [ -d "${PROD_DIR}" ]; then
        FCOUNT=$(find "${PROD_DIR}" -type f ! -name '.*' | wc -l | tr -d ' ')
        echo ""
        echo "Products: ${FCOUNT} files in ${PROD_DIR}"
    else
        echo ""
        echo "Products: none"
    fi
    echo ""
    echo "===== End Event ====="
    exit 0
fi

# -- Show queue state --
if [ -n "${SHOW_QUEUE}" ]; then
    echo "===== Queue State ====="
    QUEUED_COUNT=0
    if [ -d "${EVENTS_DIR}" ]; then
        for event_dir in "${EVENTS_DIR}"/*/; do
            [ -d "${event_dir}" ] || continue
            STATUS_FILE="${event_dir}.shakemap-service/requeststatus.json"
            [ -f "${STATUS_FILE}" ] || continue
            STATUS=$(python3 -c "import json; print(json.load(open('${STATUS_FILE}')).get('status',''))" 2>/dev/null || echo "")
            if [ "${STATUS}" = "QUEUED" ]; then
                EVENT_ID=$(basename "${event_dir}")
                QUEUED_AT=$(python3 -c "import json; print(json.load(open('${STATUS_FILE}')).get('queued_at','?'))" 2>/dev/null || echo "?")
                echo "  ${EVENT_ID}  queued_at=${QUEUED_AT}"
                QUEUED_COUNT=$((QUEUED_COUNT + 1))
            fi
        done
    fi
    echo ""
    echo "Pending count: ${QUEUED_COUNT}"
    echo ""
    echo "===== End Queue ====="
    exit 0
fi

# -- List all events --
echo "===== Events ====="
echo ""

if [ ! -d "${EVENTS_DIR}" ]; then
    echo "  No events directory found at ${EVENTS_DIR}"
    echo ""
    echo "===== End Events ====="
    exit 0
fi

# Header
printf "%-35s %-20s %-25s %-8s %s\n" "EVENT_ID" "STATUS" "SUBMITTED_AT" "ATTEMPT" "PRODUCTS"
printf "%-35s %-20s %-25s %-8s %s\n" "--------" "------" "------------" "-------" "--------"

EVENT_COUNT=0
SHOWN_COUNT=0

for event_dir in "${EVENTS_DIR}"/*/; do
    [ -d "${event_dir}" ] || continue
    EVENT_ID=$(basename "${event_dir}")
    STATUS_FILE="${event_dir}.shakemap-service/requeststatus.json"

    if [ ! -f "${STATUS_FILE}" ]; then
        printf "%-35s %-20s %-25s %-8s %s\n" "${EVENT_ID}" "NO_STATUS_FILE" "-" "-" "-"
        EVENT_COUNT=$((EVENT_COUNT + 1))
        continue
    fi

    # Read status via Python for reliable JSON parsing
    read -r STATUS SUBMITTED_AT ATTEMPT MAX_ATT <<< $(python3 -c "
import json
with open('${STATUS_FILE}') as f:
    d = json.load(f)
print(d.get('status','?'), d.get('submitted_at','?'), d.get('current_attempt',0), d.get('max_attempts',3))
" 2>/dev/null || echo "? ? 0 3")

    # Apply status filter if specified
    if [ -n "${STATUS_FILTER}" ]; then
        FILTER_UPPER=$(echo "${STATUS_FILTER}" | tr '[:lower:]' '[:upper:]')
        if [ "${STATUS}" != "${FILTER_UPPER}" ]; then
            EVENT_COUNT=$((EVENT_COUNT + 1))
            continue
        fi
    fi

    # Check for products
    PROD="no"
    PROD_DIR="${PRODUCTS_DIR}/${EVENT_ID}"
    if [ -d "${PROD_DIR}" ]; then
        FCOUNT=$(find "${PROD_DIR}" -type f ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
        PROD="${FCOUNT} files"
    fi

    printf "%-35s %-20s %-25s %-8s %s\n" "${EVENT_ID}" "${STATUS}" "${SUBMITTED_AT}" "${ATTEMPT}/${MAX_ATT}" "${PROD}"
    EVENT_COUNT=$((EVENT_COUNT + 1))
    SHOWN_COUNT=$((SHOWN_COUNT + 1))
done

echo ""
if [ -n "${STATUS_FILTER}" ]; then
    echo "Total events: ${EVENT_COUNT}  |  Shown (status=${STATUS_FILTER}): ${SHOWN_COUNT}"
else
    echo "Total events: ${EVENT_COUNT}"
fi
echo ""
echo "===== End Events ====="
