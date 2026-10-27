#!/bin/bash
# Container entrypoint (Fly.io). Site code comes from the image on every boot;
# state (schedule, archives, histories, logs) lives on the /data volume.
# If any poller dies, exit non-zero so Fly restarts the machine.
set -u
mkdir -p "$DOCS_DIR/archive" "$DATA_DIR"

# the dashboard always comes from the deployed image; the poller owns watchlist.json
cp /app/docs/index.html "$DOCS_DIR/"
cp -n /app/docs/watchlist.json "$DOCS_DIR/" 2>/dev/null || true
# first boot only: seed the schedule and archives collected on the Mac
cp -n /app/docs/*_games.json "$DOCS_DIR/" 2>/dev/null || true
cp -rn /app/docs/archive/. "$DOCS_DIR/archive/" 2>/dev/null || true

cd /app/live
python3 -u poller.py --poll 180 &
python3 -u cfb_poller.py --poll 120 &
python3 -u cfb_poller.py --sport nfl --poll 180 &
wait -n
echo "a poller exited; stopping so Fly restarts the machine"
exit 1
