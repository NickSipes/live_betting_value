#!/bin/sh
# Copy everything the live system has collected (the Fly volume /data) into data/fly/.
# Re-run any time; files are overwritten with the server's current copy, nothing local is deleted.
# Schemas: data/README.md
# Usage: sh analysis/pull_fly.sh
set -e
APP=live-betting-value
cd "$(dirname "$0")/.."
mkdir -p data/fly
fly ssh console -a $APP -C "tar czf /tmp/lbv_data.tgz -C /data ." >/dev/null
rm -f data/fly/lbv_data.tgz
fly sftp get -a $APP /tmp/lbv_data.tgz data/fly/lbv_data.tgz >/dev/null
fly ssh console -a $APP -C "rm /tmp/lbv_data.tgz" >/dev/null
tar xzf data/fly/lbv_data.tgz -C data/fly && rm data/fly/lbv_data.tgz
rm -f data/fly/docs/index.html    # the dashboard page, not data
date -u +%Y-%m-%dT%H:%M:%SZ > data/fly/PULLED_AT
find data/fly -type f | sort
