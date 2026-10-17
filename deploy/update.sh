#!/usr/bin/env bash
# Update an installed Casa to the latest version and restart it.
#
#   on the Pi:        ~/casa/deploy/update.sh
#   from a computer:  ssh <your-pi> '~/casa/deploy/update.sh'
#
# config.toml and data/ are not tracked by git, so settings and the HomeKit
# pairing survive updates.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

before="$(git rev-parse HEAD)"
git pull --ff-only -q
after="$(git rev-parse HEAD)"
if [ "$before" = "$after" ] && [ "${1:-}" != "--force" ]; then
  echo "Already up to date ($(git log -1 --format='%h %s'))."
  exit 0
fi
if ! git diff --quiet "$before" "$after" -- requirements.txt; then
  .venv/bin/python -m pip install -q -r requirements.txt
fi
# the installer may have gained steps (packages, helpers); it is safe to re-run
if ! git diff --quiet "$before" "$after" -- deploy/install-pi.sh deploy/casa-mount; then
  echo "deploy/install-pi.sh changed: re-run it to apply system changes."
fi
sudo systemctl restart casa
echo "Updated to $(git log -1 --format='%h %s'); restarted."
