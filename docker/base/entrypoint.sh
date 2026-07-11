#!/usr/bin/env bash
# Container entrypoint shared by every Samus workcell image.
# tini (declared in the base image ENTRYPOINT) reaps zombies; this script
# normalises env + execs the workcell command in-place.
set -euo pipefail

umask 0027

# Ensure /opt/samus is importable as the top-level package root.
export PYTHONPATH="${PYTHONPATH:-}:/opt/samus"

# Cloud Run injects $PORT; default to 8080 for Compose / local runs.
export PORT="${PORT:-8080}"

exec "$@"
