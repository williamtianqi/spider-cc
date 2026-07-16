#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <run-dir> [limit]" >&2
  exit 1
fi

RUN_DIR="$1"
LIMIT="${2:-5}"
python3 - <<'PY' "$RUN_DIR" "$LIMIT"
import json
import sys
from pathlib import Path
run_dir = Path(sys.argv[1])
limit = int(sys.argv[2])
path = run_dir / 'pages.jsonl'
if not path.exists():
    path = run_dir / 'extracted_text.jsonl'
for idx, line in enumerate(path.open('r', encoding='utf-8'), 1):
    if idx > limit:
        break
    row = json.loads(line)
    print(json.dumps(row, ensure_ascii=False, indent=2))
PY
