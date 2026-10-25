#!/bin/bash
# Re-fetch nflverse data (games + play-by-play)
set -e
cd "$(dirname "$0")/data"
curl -sL "https://github.com/nflverse/nfldata/raw/master/data/games.csv" -o games.csv
for y in 2024 2025 2026; do
  curl -sL "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_${y}.csv.gz" -o "pbp_${y}.csv.gz"
done
ls -lh
