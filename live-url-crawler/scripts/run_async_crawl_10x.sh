#!/usr/bin/env bash
set -euo pipefail

# 10x 吞吐异步爬取 - 目标 1500+ pages/s
# 前置条件: pip install aiohttp
# 使用已有 seed TSV 或先跑 discovery

SEEDS_TSV="${1:-cc_seed_sites_100x_en.tsv}"
OUTPUT_ROOT="${2:-data/runs/cc_async_10x}"
MAX_SITES="${3:-20000}"
PAGES_PER_SITE="${4:-100000}"
MAX_CONCURRENT="${5:-2048}"
ACTIVE_SITES="${6:-512}"

echo "=== Async 10x Crawl ==="
echo "Seeds: $SEEDS_TSV"
echo "Output: $OUTPUT_ROOT"
echo "Max sites: $MAX_SITES"
echo "Pages/site: $PAGES_PER_SITE"
echo "Global concurrency: $MAX_CONCURRENT"
echo "Active sites: $ACTIVE_SITES"
echo ""

mkdir -p "$OUTPUT_ROOT"

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
  --stats-jsonl cc_async_run_stats.jsonl \
  --partial-jsonl cc_async_pages_partial.jsonl \
  --latest-json cc_async_run_latest.json \
  "${@:7}"
