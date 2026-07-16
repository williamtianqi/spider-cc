#!/usr/bin/env bash
set -euo pipefail

# 完整 pipeline: CC 站点发现 -> 异步 live 爬取 -> 规模推算
# 用法: bash scripts/run_full_pipeline.sh [crawl_id] [max_wet_files] [max_sites]

CRAWL_ID="${1:-CC-MAIN-2025-08}"
MAX_WET_FILES="${2:-100}"
MAX_SITES="${3:-50000}"
PAGES_PER_SITE="${4:-100000}"
MAX_CONCURRENT="${5:-2048}"
ACTIVE_SITES="${6:-512}"
SUFFIX="full_$(date +%Y%m%d_%H%M%S)"

SEEDS_TSV="cc_seed_sites_${SUFFIX}.tsv"
SITES_JSONL="cc_sites_${SUFFIX}.jsonl"
OUTPUT_ROOT="data/runs/cc_live_${SUFFIX}"
STATS_JSONL="cc_live_run_stats_${SUFFIX}.jsonl"
PARTIAL_JSONL="cc_live_pages_${SUFFIX}_partial.jsonl"
LATEST_JSON="cc_live_run_latest_${SUFFIX}.json"

echo "=== Phase 1: Common Crawl Site Discovery ==="
echo "Crawl: $CRAWL_ID, WET files: $MAX_WET_FILES, Target sites: $MAX_SITES"

python3 src/common_crawl_site_discovery.py \
  --crawl-id "$CRAWL_ID" \
  --max-wet-files "$MAX_WET_FILES" \
  --spread-wet-paths \
  --max-sites "$MAX_SITES" \
  --max-sites-per-wet 50000 \
  --max-records-per-wet 200000 \
  --workers 8 \
  --timeout 75 \
  --english-domain-only \
  --output-sites-jsonl "$SITES_JSONL" \
  --output-seeds-tsv "$SEEDS_TSV" \
  --output-manifest-jsonl "cc_site_discovery_manifest_${SUFFIX}.jsonl" \
  --summary-json "cc_site_discovery_summary_${SUFFIX}.json" \
  --progress-json "cc_site_discovery_progress_${SUFFIX}.json"

echo ""
echo "=== Phase 2: Async Live Crawl ==="
echo "Output: $OUTPUT_ROOT, Concurrency: $MAX_CONCURRENT, Active sites: $ACTIVE_SITES"

python3 src/async_pipeline_crawler.py \
  --seeds-tsv "$SEEDS_TSV" \
  --output-root "$OUTPUT_ROOT" \
  --max-sites "$MAX_SITES" \
  --pages-per-site "$PAGES_PER_SITE" \
  --max-discovered 2000000 \
  --max-depth 50 \
  --max-concurrent "$MAX_CONCURRENT" \
  --max-per-host 8 \
  --active-sites "$ACTIVE_SITES" \
  --timeout 8 \
  --max-page-bytes 2000000 \
  --stats-jsonl "$STATS_JSONL" \
  --partial-jsonl "$PARTIAL_JSONL" \
  --latest-json "$LATEST_JSON"

echo ""
echo "=== Phase 3: Scale Estimation ==="

python3 src/estimate_scale.py \
  --discovery-summary "cc_site_discovery_summary_${SUFFIX}.json" \
  --run-root "$OUTPUT_ROOT" \
  --run-stats "$STATS_JSONL" \
  --output "cc_scale_estimate_${SUFFIX}.json"

echo ""
echo "=== Done ==="
echo "Pages JSONL: $PARTIAL_JSONL"
echo "Stats: $STATS_JSONL"
echo "Scale estimate: cc_scale_estimate_${SUFFIX}.json"
