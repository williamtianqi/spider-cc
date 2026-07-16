#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <run-dir>" >&2
  exit 1
fi

python3 src/monitor_run.py --run-dir "$1" --watch --interval 5 --tail 10
