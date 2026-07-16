#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <seeds.tsv> [parallel_jobs] [pages_per_seed] [output_root]" >&2
  echo "TSV columns: seed_url<TAB>scope_domain_or_host<TAB>output_name" >&2
  exit 1
fi

SEEDS_FILE="$1"
PARALLEL_JOBS="${2:-128}"
PAGES_PER_SEED="${3:-500000}"
OUTPUT_ROOT="${4:-data/runs/multi_seed_turbo}"
FETCH_WORKERS="${FETCH_WORKERS:-64}"
MAX_HOST_WORKERS="${MAX_HOST_WORKERS:-32}"
MAX_IN_FLIGHT_FETCH="${MAX_IN_FLIGHT_FETCH:-2048}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-6}"
FORCE_RECRAWL="${FORCE_RECRAWL:-0}"

mkdir -p "$OUTPUT_ROOT"


run_one_seed() {
  local seed_url="$1"
  local scope="$2"
  local output_name="$3"
  local output_dir="$OUTPUT_ROOT/$output_name"

  if [[ "$FORCE_RECRAWL" != "1" && -f "$output_dir/summary.json" ]]; then
    if python3 - "$output_dir/summary.json" <<'PY'
import json
import sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    row = json.loads(p.read_text(encoding='utf-8'))
except Exception:
    sys.exit(1)
sys.exit(0 if row.get('site_crawl_complete') and row.get('stopped_reason') == 'frontier_exhausted' else 1)
PY
    then
      echo "skip_completed $output_name"
      return 0
    fi
  fi
  if [[ "$FORCE_RECRAWL" != "1" && -f "$output_dir/progress.json" && ! -f "$output_dir/summary.json" ]]; then
    echo "skip_active_or_incomplete $output_name"
    return 0
  fi

  mkdir -p "$output_dir"

  python3 src/pipeline_domain_crawler.py \
    --seed-url "$seed_url" \
    --allowed-domain "$scope" \
    --include-subdomains \
    --output-dir "$output_dir" \
    --max-pages "$PAGES_PER_SEED" \
    --max-discovered 2000000 \
    --max-depth 50 \
    --max-sitemaps 2000 \
    --fetch-workers "$FETCH_WORKERS" \
    --extract-workers 1 \
    --max-host-workers "$MAX_HOST_WORKERS" \
    --max-in-flight-fetch "$MAX_IN_FLIGHT_FETCH" \
    --max-in-flight-extract 1 \
    --progress-interval 1000 \
    --extract-mode regex-inline \
    --link-mode regex \
    --robots-check-stage fetch \
    --no-write-links \
    --timeout "$TIMEOUT_SECONDS" \
    --max-page-bytes 2000000 \
    --max-sitemap-bytes 50000000 \
    > "$output_dir/stdout.log" 2> "$output_dir/stderr.log"
}

while IFS=$'\t' read -r seed_url scope output_name; do
  if [[ -z "${seed_url// }" || "${seed_url:0:1}" == "#" ]]; then
    continue
  fi
  if [[ -z "${scope:-}" || -z "${output_name:-}" ]]; then
    echo "Invalid TSV row: $seed_url $scope $output_name" >&2
    exit 1
  fi

  run_one_seed "$seed_url" "$scope" "$output_name" &

  while (( $(jobs -pr | wc -l | tr -d ' ') >= PARALLEL_JOBS )); do
    sleep 1
  done
done < "$SEEDS_FILE"

wait
