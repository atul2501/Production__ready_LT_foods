#!/usr/bin/env bash
set -euo pipefail

# Copies the shared app log (written by both api and worker to the same named volume)
# out to ./logs/app.log on the host. Needed because Docker Desktop on macOS restricts
# bind-mounting host paths under ~/Desktop without an explicit Files & Folders permission
# grant (System Settings > Privacy & Security > Files and Folders > Docker) - once that's
# granted, docker-compose.yml can switch back to a live bind mount and this script becomes
# unnecessary. Until then, run this whenever you want an up-to-date copy on disk.

cd "$(dirname "$0")/.."
mkdir -p logs
docker compose cp api:/var/log/app/app.log ./logs/app.log
echo "Synced to $(pwd)/logs/app.log ($(wc -l < logs/app.log) lines)"
