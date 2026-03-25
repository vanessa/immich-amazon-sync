#!/bin/sh
set -e

INTERVAL_HOURS="${SYNC_INTERVAL_HOURS:-6}"
INTERVAL_SECONDS=$((INTERVAL_HOURS * 3600))

echo "Immich -> Amazon Photos sync starting. Interval: every ${INTERVAL_HOURS}h"

while true; do
    echo "=== Sync run: $(date -Iseconds) ==="
    python3 /app/immich_sync.py || echo "Sync finished with errors (will retry next run)"
    echo "=== Next run in ${INTERVAL_HOURS}h ==="
    sleep "$INTERVAL_SECONDS"
done
