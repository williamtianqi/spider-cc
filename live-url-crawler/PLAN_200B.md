# 200 亿英文网页采集方案

## 目标

从 Common Crawl 获取英文域名，全站 live crawl 抓取正文，产出 200 亿条 unique 去重记录。

## 关键数字

| 指标 | 数值 |
|------|------|
| CC 全部 crawl | 124 次 (2008-2026) |
| CC 英文 unique URLs | ~370 亿 |
| CC 英文域名 | ~3 亿 |
| 目标条数 | 200 亿 (CC 已有的 53%) |
| 每条正文 | ~2.2 KB / 547 tokens |
| 总正文 | ~48 TB |
| 总 tokens | ~11T |
| 存储 (JSONL 全文) | ~50 TB |

## 总体架构

```
Phase 1: 域名发现 (CC WET → seed 列表)
    ↓
Phase 2: 全站 live crawl (seed → pages.jsonl)
    ↓
Phase 3: 合并去重 (content hash dedup → final output)
```

---

## Phase 1: 域名发现

### 目标

从多次 CC crawl 中提取 2 亿+ 英文域名 seed 列表。

### 方法

从 42 次近 5 年的 CC crawl (2021-2026) 各下载 WET 文件，提取 `WARC-Target-URI` 中的英文域名。

### 参数

```bash
# 每次 crawl 处理 50 个 WET 文件 (spread 采样)
# 42 次 × 50 WET × 16K hosts/WET = ~33M raw
# 去重后约 20-30M unique 域名/批次
# 分 5-10 批次执行，合并去重到 200M+

CRAWL_IDS=(
  CC-MAIN-2025-51 CC-MAIN-2025-47 CC-MAIN-2025-43 CC-MAIN-2025-38
  CC-MAIN-2025-33 CC-MAIN-2025-30 CC-MAIN-2025-26 CC-MAIN-2025-21
  CC-MAIN-2025-18 CC-MAIN-2025-13 CC-MAIN-2025-08 CC-MAIN-2025-05
  CC-MAIN-2024-51 CC-MAIN-2024-46 CC-MAIN-2024-42 CC-MAIN-2024-38
  CC-MAIN-2024-33 CC-MAIN-2024-30 CC-MAIN-2024-26 CC-MAIN-2024-22
  CC-MAIN-2024-18 CC-MAIN-2024-10
  CC-MAIN-2023-50 CC-MAIN-2023-40 CC-MAIN-2023-23 CC-MAIN-2023-14 CC-MAIN-2023-06
  CC-MAIN-2022-49 CC-MAIN-2022-40 CC-MAIN-2022-33 CC-MAIN-2022-27 CC-MAIN-2022-21 CC-MAIN-2022-05
  CC-MAIN-2021-49 CC-MAIN-2021-43 CC-MAIN-2021-39 CC-MAIN-2021-31 CC-MAIN-2021-25 CC-MAIN-2021-21 CC-MAIN-2021-17 CC-MAIN-2021-10 CC-MAIN-2021-04
)
```

### 执行命令

```bash
# 单次 crawl 发现 (每次约 10 分钟)
python3 src/common_crawl_site_discovery.py \
  --crawl-id CC-MAIN-2025-08 \
  --max-wet-files 100 \
  --spread-wet-paths \
  --max-sites 500000 \
  --max-sites-per-wet 50000 \
  --max-records-per-wet 200000 \
  --workers 8 \
  --timeout 90 \
  --english-domain-only \
  --output-sites-jsonl cc_sites_${CRAWL_ID}.jsonl \
  --output-seeds-tsv cc_seeds_${CRAWL_ID}.tsv \
  --summary-json cc_discovery_${CRAWL_ID}.json
```

### 合并去重

```bash
# 合并所有 crawl 的 seeds，按 domain 去重
cat cc_seeds_CC-MAIN-*.tsv | grep -v "^#" | sort -t$'\t' -k2,2 -u > cc_seeds_all_unique.tsv
wc -l cc_seeds_all_unique.tsv  # 目标: 200M+
```

### 产出

- `cc_seeds_all_unique.tsv` — 去重后的 seed 列表 (seed_url, scope, output_name)
- 预期: 1.5-2 亿行

---

## Phase 2: 全站 Live Crawl

### 目标

对 seed 列表中的每个域名做全站 BFS 抓取，每站产出 pages.jsonl。

### 机器规划

| 配置 | 数量 | 速度 | 总速度 |
|------|------|------|--------|
| 8-core VPS, 1Gbps | 16 台 | 300 p/s/台 | 4,800 p/s |

200 亿 ÷ 4800 p/s ÷ 86400 = **48 天**

更激进: 32 台 → **24 天**

### 每台机器执行

```bash
# 分片: 200M 域名 / 16 台 = 12.5M 域名/台
# 按 shard ID 分

python3 src/multiproc_async_crawler.py \
  --seeds-tsv cc_seeds_shard_${SHARD_ID}.tsv \
  --output-root /data/crawl/shard_${SHARD_ID} \
  --workers 8 \
  --max-sites 12500000 \
  --pages-per-site 50000 \
  --max-discovered 500000 \
  --max-depth 50 \
  --max-concurrent-per-worker 1024 \
  --max-per-host 16 \
  --active-sites-per-worker 256 \
  --timeout 6 \
  --stats-jsonl /data/crawl/stats_shard_${SHARD_ID}.jsonl \
  --partial-jsonl /data/crawl/pages_shard_${SHARD_ID}.jsonl \
  --latest-json /data/crawl/latest_shard_${SHARD_ID}.json
```

### 单站退出条件

- `frontier_exhausted` — 全站抓完 (理想)
- `max_pages_reached` (50000) — 大站 cap
- 超时 30 分钟 — 强制结束

### 产出

- 每站: `pages.jsonl` + `summary.json`
- 每台: `pages_shard_N.jsonl` (增量合并)
- 每条记录: `{url, title, text_length, content_hash, text_preview, depth}`

---

## Phase 3: 合并去重

### 目标

跨站点、跨分片的 content hash 去重，产出最终 200 亿条。

### 方法

```bash
# 1. 收集所有 shard 的 partial JSONL
# 2. 按 content_hash 全局去重
# 3. 输出 final JSONL

# 简单方法: sort + uniq by content_hash
cat /data/crawl/pages_shard_*.jsonl | \
  python3 -c "
import sys, json
seen = set()
for line in sys.stdin:
    d = json.loads(line)
    h = d.get('content_hash')
    if h and h not in seen:
        seen.add(h)
        sys.stdout.write(line)
" > cc_final_200b.jsonl
```

大规模时用 Spark/Hadoop 或按 hash 前缀分桶并行去重。

---

## 执行时间线

| 阶段 | 耗时 | 说明 |
|------|------|------|
| Phase 1: 域名发现 | 1-2 天 | 42 crawl × 10 min/crawl, 可并行 |
| Phase 2: Live crawl | 24-48 天 | 16-32 台 VPS 并行 |
| Phase 3: 去重合并 | 1-3 天 | 按 hash 分桶并行 |
| **总计** | **30-50 天** | |

---

## 成本估算

| 项目 | 数量 | 单价 | 总计 |
|------|------|------|------|
| VPS (8c16g, 1Gbps) | 16 台 × 48 天 | $0.10/h | $1,840 |
| 存储 (50TB SSD) | 分布在 16 台 | $0.08/GB/月 | $4,000/月 |
| 带宽 (出口免费) | 含在 VPS | - | - |
| **总计** | | | **~$6,000** |

用 Spot/抢占式实例可降 60-70%: **~$2,000**

---

## 关键脚本

| 脚本 | 用途 |
|------|------|
| `src/common_crawl_site_discovery.py` | CC WET → 域名发现 |
| `src/multiproc_async_crawler.py` | 多进程异步全站抓取 |
| `src/async_pipeline_crawler.py` | 单进程异步版 (备选) |
| `src/monitor_cc_live_run.py` | 实时监控 |
| `src/estimate_scale.py` | 规模推算 |
| `scripts/run_multiproc_10x.sh` | 单机 8-worker 启动 |
| `scripts/run_full_pipeline.sh` | 完整 pipeline |

---

## 监控

```bash
# 每台机器
cat /data/crawl/latest_shard_N.json

# 汇总
for i in $(seq 0 15); do
  ssh node-$i "cat /data/crawl/latest_shard_${i}.json"
done | python3 -c "
import json, sys
total_fetched = 0
total_unique = 0
for line in sys.stdin:
    d = json.loads(line)
    total_fetched += d.get('total_fetched', 0)
    total_unique += d.get('total_unique_text', 0)
print(f'Total: {total_fetched:,} fetched, {total_unique:,} unique')
print(f'Progress: {total_unique/20_000_000_000*100:.2f}%')
"
```

---

## 风险与应对

| 风险 | 应对 |
|------|------|
| CC WET 下载失败 | 3 次重试 + backoff (已实现) |
| 站点封锁/验证码 | 429/5xx/超时重试 + Retry-After + challenge 页检测 + `likely_blocked` 标记 + curl_cffi TLS/JA3 浏览器指纹伪装 (已实现, 见 `PIPELINE_ANALYSIS.md` 第 9 节) |
| 大站跑不完 | 50000 页 cap + 30min 超时 |
| 内容重复 | content_hash 全局去重 |
| IP 被封 | 分散到 16+ IP, per-host 限流 |
| 存储不够 | 增量合并, 完成站点立即上传/删除本地 |
| 域名发现不够 200M | 增加 WET 采样数 (100→500/crawl) |

---

## 验证检查点

1. Phase 1 完成后: 验证域名数 ≥ 150M
2. 单台跑 1 天后: 验证速度 ≥ 250 p/s, 日产 ≥ 2000 万条
3. 第 7 天: 验证总量线性增长, 无瓶颈
4. 第 30 天: 验证去重后 ≥ 150 亿, 按时完成

---

## 一句话

**42 次 CC crawl 发现 2 亿英文域名 → 16 台机器全站抓 48 天 → content hash 去重 → 200 亿条英文网页正文。**
