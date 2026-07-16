# Live URL Crawler

面向 AI 搜索数据源的真实站点最新抓取 demo。

这个项目聚焦 live crawl，不把 Common Crawl 当最终正文来源。Common Crawl 只适合做站点发现、URL pattern 发现和冷启动。

## 当前能力

- 域名级内部链接递归发现
- 通用 sitemap 发现
- robots.txt 合规检查
- sitemap index 递归解析
- RSS/Atom feed URL 发现
- 页面内链 BFS 扩展
- URL 规范化
- URL hash 去重
- 二进制/静态资源过滤
- trafilatura 开源正文抽取
- exact content hash 去重
- 并发抓取
- 单页面最大字节限制
- 输出 discovered URLs、links、manifest、正文抽取结果、剩余 frontier

## 重要边界

当前 sample run 不是无遗漏全站抓取。

已验证的是：

```text
发现候选 URL: 549
请求页面: 80
成功抽取唯一正文页: 80
```

如果要尽量覆盖某个站点，需要增大：

```text
--max-sitemaps
--max-candidates
--link-discovery-pages
--max-pages
```

并且需要持久化 frontier、失败重试、定期增量抓取和更多 URL pattern 控制。

## 安装

```bash
python3 -m pip install --index-url https://pypi.org/simple -r requirements.txt
```

如果你的 pip 默认源是私有源且凭证失效，需要显式加 `--index-url https://pypi.org/simple`。

## 快速运行

域名级递归抓取：

```bash
bash scripts/run_domain_docs_python_small.sh
```

高吞吐主从流水线抓取：

```bash
bash scripts/run_pipeline_docs_python_fast.sh
```

50 万页 turbo 抓取模板：

```bash
bash scripts/run_pipeline_500k_from_seed.sh https://example.com/ data/runs/example_500k example.com
```

多 seed 并行 turbo 抓取，适合保留正文时横向扩展吞吐：

```bash
bash scripts/run_pipeline_multi_seed_turbo.sh configs/multi_seed_example.tsv 8 500000 data/runs/multi_seed_turbo
```

从 Common Crawl 取站点 seed，再由本项目 live crawler 自己找 sitemap/feed/正文，最终合并到当前目录 JSONL：

```bash
bash scripts/run_common_crawl_sites_500k.sh CC-MAIN-2025-08 500 32 100000 data/runs/cc_live_sites_full cc_live_pages.jsonl 50
```

小规模验证：

```bash
bash scripts/run_common_crawl_sites_500k.sh CC-MAIN-2025-08 5 2 2 data/runs/cc_live_sites_smoke cc_live_pages_smoke.jsonl 5
```

查看正文 JSONL 样例：

```bash
bash scripts/show_pages_sample.sh data/runs/pipeline_docs_python_fast 5
```

候选优先级抓取：

```bash
bash scripts/run_docs_python_sample.sh
```

监控运行目录：

```bash
bash scripts/monitor_run.sh data/runs/domain_docs_python_fast
```

按本机实测最快 `84.96 pages/s` 估算，24 小时约 `7.34M pages/day`，抓 `500k` 页约 `1.64` 小时。实际吞吐取决于目标站点、host 限流、网络、页面大小和是否使用 `trafilatura`。

如果要在保留正文的前提下达到当前速度的 `50x`，目标约 `4,248 pages/s`，约 `367M pages/day`。这需要多 seed、多域名或多机器横向分片；单公共域名不应直接承受这个请求量。`scripts/run_pipeline_multi_seed_turbo.sh` 会为每个 seed 启动独立 pipeline，分别输出各自的 `pages.jsonl`。

Common Crawl 在这里只作为站点/域名发现来源：读取 WET header 的 `WARC-Target-URI` 生成 `cc_sites.jsonl` 和 `cc_seed_sites.tsv`。最终正文不是 Common Crawl 里的正文，而是本项目根据这些 seed 重新 live 抓取 robots/sitemap/feed/页面后抽取，并合并输出到当前目录的 `cc_live_pages.jsonl`。

正式数据要求每个站点尽量全量抓取：脚本给每站很高的 `max-pages` 安全上限。每个站点的 `summary.json` 会写 `site_crawl_complete` 和 `stopped_reason`；只有 `site_crawl_complete=true` 且 `stopped_reason=frontier_exhausted` 才表示该站点 frontier 已抓空。汇总文件 `cc_live_site_summaries.jsonl` 会列出每站状态。

## 放量运行示例

域名级全链接抓取：

```bash
python3 src/domain_link_crawler.py \
  --seed-url https://docs.python.org/3/ \
  --allowed-host docs.python.org \
  --output-dir data/runs/domain_docs_python_large \
  --max-pages 5000 \
  --max-discovered 200000 \
  --max-depth 20 \
  --max-sitemaps 500 \
  --max-workers 32 \
  --max-host-workers 16 \
  --batch-size 500 \
  --progress-interval 100 \
  --timeout 20 \
  --max-page-bytes 2000000 \
  --max-sitemap-bytes 50000000
```

跨子域抓取：

```bash
python3 src/domain_link_crawler.py \
  --seed-url https://example.com/ \
  --allowed-domain example.com \
  --include-subdomains \
  --output-dir data/runs/example_com \
  --max-pages 5000 \
  --max-discovered 200000
```

## 输出文件

```text
data/runs/<site>/summary.json
本次抓取汇总、sitemap 发现结果、URL 发现数、抓取数、剩余 frontier

data/runs/<site>/discovered_urls.csv
所有已发现并入队的 URL

data/runs/<site>/links.csv
页面链接边，from_url -> to_url

data/runs/<site>/manifest.jsonl
每个请求的抓取状态、content-type、正文长度、content_hash

data/runs/<site>/pages.jsonl
高吞吐 pipeline 模式的正文抽取结果，每行一个页面 JSON

data/runs/<site>/extracted_text.jsonl
普通模式正文抽取结果，默认只保留 text_preview，不保存全文

data/runs/<site>/frontier_remaining.jsonl
达到限制后尚未抓取的剩余 URL

data/runs/<site>/progress.json
运行中的实时进度快照，包含抓取量、速度、frontier、成功数

data/runs/<site>/crawl.log
运行日志，周期性输出吞吐和进度
```

## 保存完整正文

默认不保存完整正文，减少磁盘和 IO。

如果需要保存完整正文：

```bash
python3 src/optimized_live_crawler.py \
  --base-url https://docs.python.org/3/ \
  --allowed-host docs.python.org \
  --include-path-prefix /3/ \
  --output-dir data/runs/docs_python_org_fulltext \
  --max-pages 100 \
  --write-full-text
```

## 保存原始 HTML

默认不保存 HTML，减少磁盘。

如果需要保存 HTML：

```bash
python3 src/optimized_live_crawler.py \
  --base-url https://docs.python.org/3/ \
  --allowed-host docs.python.org \
  --include-path-prefix /3/ \
  --output-dir data/runs/docs_python_org_html \
  --max-pages 100 \
  --save-html
```

## 当前样例结果

已整理到：

```text
data/sample_run/docs_python_org/
```

这次样例结果：

```text
候选 URL: 549
请求页面: 80
唯一正文页: 80
耗时: 47.87 秒
正文抽取器: trafilatura
```
