#!/usr/bin/env bash
set -euo pipefail

# 多进程 + 异步混合 crawler
# 目标: 8 workers × ~150-200 p/s = 1200-1600 p/s (需要独占网络)
# 实际单机网络瓶颈约 300-500 p/s，需要排除其他爬虫进程

SEEDS_TSV="${1:-cc_seed_sites_100x_en.tsv}"
OUTPUT_ROOT="${2:-data/runs/cc_multiproc_10x}"
WORKERS="${3:-8}"
MAX_SITES="${4:-20000}"
PAGES_PER_SITE="${5:-100000}"

echo "=== Multi-Process Async Crawler (10x target) ==="
echo "Seeds: $SEEDS_TSV"
echo "Output: $OUTPUT_ROOT"
echo "Workers: $WORKERS"
echo "Max sites: $MAX_SITES"
echo "Pages/site: $PAGES_PER_SITE"
echo ""
echo "注意: 最大化吞吐需要独占网络，请先停止其他爬虫进程"
echo ""

ulimit -n 65536 2>/dev/null || ulimit -n 10240 2>/dev/null || true
echo "File descriptor limit: $(ulimit -n)"

python3 src/multiproc_async_crawler.py \
  --seeds-tsv "$SEEDS_TSV" \
  --output-root "$OUTPUT_ROOT" \
  --workers "$WORKERS" \
  --max-sites "$MAX_SITES" \
  --pages-per-site "$PAGES_PER_SITE" \
  --max-discovered 2000000 \
  --max-depth 50 \
  --max-concurrent-per-worker 1024 \
  --max-per-host 8 \
  --active-sites-per-worker 256 \
  --timeout 5 \
  --stats-jsonl cc_multiproc_10x_stats.jsonl \
  --partial-jsonl cc_multiproc_10x_partial.jsonl \
  --latest-json cc_multiproc_10x_latest.json
