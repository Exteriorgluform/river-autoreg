#!/usr/bin/env bash
# Paced wave runner: bash river_wave.sh [N] [START_IDX] [PHONE_PROVIDER]
# Aborts automatically when WorkOS Radar IP-block trips (data/radar_block).
set -u
cd "$(dirname "$0")"
N=${1:-5}
START=${2:-0}
PHONE=${3:-}
PY=${PYTHON:-python}
ARGS=("$N" --start "$START" --imap-timeout 780 --human)
if [ -n "$PHONE" ]; then ARGS+=(--phone "$PHONE"); fi
"$PY" river_reg.py "${ARGS[@]}" 2>&1 | tee -a data/wave_$(date +%Y%m%d_%H%M%S).log
