#!/usr/bin/env bash
set -euo pipefail

CRAWL_ID="${1:-CC-MAIN-2025-08}"
MAX_SITES="${2:-5000}"
PARALLEL_JOBS="${3:-128}"
PAGES_PER_SITE="${4:-100000}"
OUTPUT_ROOT="${5:-data/runs/cc_live_sites_100x}"
FINAL_JSONL="${6:-cc_live_pages_100x.jsonl}"
MAX_WET_FILES="${7:-200}"

FETCH_WORKERS="${FETCH_WORKERS:-32}" \
MAX_HOST_WORKERS="${MAX_HOST_WORKERS:-16}" \
MAX_IN_FLIGHT_FETCH="${MAX_IN_FLIGHT_FETCH:-1024}" \
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-8}" \
python3 src/common_crawl_site_discovery.py \
  --crawl-id "$CRAWL_ID" \
  --max-wet-files "$MAX_WET_FILES" \
  --max-sites "$MAX_SITES" \
  --workers 16 \
  --english-domain-only \
  --output-sites-jsonl cc_sites_100x.jsonl \
  --output-seeds-tsv cc_seed_sites_100x.tsv \
  --output-manifest-jsonl cc_site_discovery_manifest_100x.jsonl \
  --summary-json cc_site_discovery_summary_100x.json

FETCH_WORKERS="${FETCH_WORKERS:-32}" \
MAX_HOST_WORKERS="${MAX_HOST_WORKERS:-16}" \
MAX_IN_FLIGHT_FETCH="${MAX_IN_FLIGHT_FETCH:-1024}" \
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-8}" \
bash scripts/run_pipeline_multi_seed_turbo.sh \
  cc_seed_sites_100x.tsv \
  "$PARALLEL_JOBS" \
  "$PAGES_PER_SITE" \
  "$OUTPUT_ROOT"

: > "$FINAL_JSONL"
find "$OUTPUT_ROOT" -name pages.jsonl -type f | sort | while IFS= read -r pages_file; do
  cat "$pages_file" >> "$FINAL_JSONL"
done

FINAL_JSONL="$FINAL_JSONL" OUTPUT_ROOT="$OUTPUT_ROOT" python3 - <<'PY'
import json
import os
from pathlib import Path
final_path = Path(os.environ['FINAL_JSONL'])
run_root = Path(os.environ['OUTPUT_ROOT'])
site_summaries = []
for summary_path in sorted(run_root.glob('*/summary.json')):
    try:
        row = json.loads(summary_path.read_text(encoding='utf-8'))
    except Exception as exc:
        row = {'summary_path': str(summary_path), 'error': repr(exc)}
    row['summary_path'] = str(summary_path)
    site_summaries.append(row)
complete_sites = sum(1 for row in site_summaries if row.get('site_crawl_complete') and row.get('stopped_reason') == 'frontier_exhausted')
capped_sites = sum(1 for row in site_summaries if row.get('stopped_reason') in {'max_pages_reached', 'max_discovered_reached'})
count = sum(row.get('pages_written') or row.get('unique_extracted_text_records') or row.get('successful_pages') or row.get('fetched_pages') or 0 for row in site_summaries)
summary = {
    'mode': '100x_site_parallel_full_site_live_crawl',
    'final_pages_jsonl': str(final_path),
    'records_estimated_from_site_summaries': count,
    'seed_sites_tsv': 'cc_seed_sites_100x.tsv',
    'site_discovery_jsonl': 'cc_sites_100x.jsonl',
    'site_summaries': len(site_summaries),
    'complete_sites': complete_sites,
    'capped_or_incomplete_sites': capped_sites,
    'note': 'A site is full-site crawled only when site_crawl_complete=true and stopped_reason=frontier_exhausted in its summary.json.',
}
Path('cc_live_pages_100x_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
Path('cc_live_site_summaries_100x.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in site_summaries), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
