#!/usr/bin/env bash
set -euo pipefail

# 极限速度模式 - 最大化单机吞吐
# 策略: 256 并行站点 + 每站 64 fetch workers + aggressive timeout
#
# 瓶颈分析:
#   - 128 并行站点时 ~158 pages/s (当前)
#   - 每个 pipeline_domain_crawler 进程: ~1-5 pages/s per site (受限于单 host)
#   - 总吞吐 ≈ 活跃大站数 × 每站速度
#   - 提速方法: 增加并行站点数 (更多域名并行 = 更多 unique hosts)
#   - 限制: macOS fd limit, RAM (~60MB/进程), CPU (regex 抽取)
#
# 目标: 500-2000 pages/s (3x-13x 提升)

SEEDS_TSV="${1:-cc_seed_sites_100x_en.tsv}"
OUTPUT_ROOT="${2:-data/runs/cc_extreme_speed}"
PARALLEL_JOBS="${3:-256}"
PAGES_PER_SITE="${4:-100000}"

echo "=== Extreme Speed Mode ==="
echo "Seeds: $SEEDS_TSV"
echo "Output: $OUTPUT_ROOT"
echo "Parallel jobs: $PARALLEL_JOBS"
echo "Pages/site: $PAGES_PER_SITE"
echo ""

# macOS fd limit 提升 (需要 sudo 才能设更高)
ulimit -n 65536 2>/dev/null || ulimit -n 10240 2>/dev/null || true
echo "File descriptor limit: $(ulimit -n)"

# 环境变量覆盖 turbo script 默认值
export FETCH_WORKERS=64
export MAX_HOST_WORKERS=32
export MAX_IN_FLIGHT_FETCH=2048
export TIMEOUT_SECONDS=5
export FORCE_RECRAWL=0

mkdir -p "$OUTPUT_ROOT"

# 启动 live crawl
bash scripts/run_pipeline_multi_seed_turbo.sh \
  "$SEEDS_TSV" \
  "$PARALLEL_JOBS" \
  "$PAGES_PER_SITE" \
  "$OUTPUT_ROOT" &
CRAWL_PID=$!

echo "Crawl started, PID=$CRAWL_PID"
echo ""

# 启动 monitor (后台)
echo "Starting monitor..."
python3 src/monitor_cc_live_run.py \
  --run-root "$OUTPUT_ROOT" \
  --partial-jsonl cc_extreme_pages_partial.jsonl \
  --stats-jsonl cc_extreme_run_stats.jsonl \
  --latest-json cc_extreme_run_latest.json \
  --merge-partial \
  --interval-seconds 30 \
  --target-records-per-minute 100000 &
MONITOR_PID=$!

echo "Monitor started, PID=$MONITOR_PID"
echo ""
echo "To check progress:"
echo "  cat cc_extreme_run_latest.json"
echo ""
echo "To stop:"
echo "  kill $CRAWL_PID $MONITOR_PID"

wait $CRAWL_PID || true
kill $MONITOR_PID 2>/dev/null || true

echo "Crawl finished."
