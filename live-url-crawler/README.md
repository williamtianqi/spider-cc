# Live URL Crawler

面向 AI 搜索数据源的真实站点最新抓取系统:给定域名/种子 URL/Common Crawl 站点列表,自动发现该站点下的可抓 URL(sitemap、feed、内链 BFS),真实 live 请求抓取,本地抽取正文,输出结构化 JSONL。

这个项目聚焦 **live crawl**,不把 Common Crawl 当最终正文来源。Common Crawl 只适合做站点/URL 发现和冷启动种子;最终正文永远是对目标站点的一次真实 live 请求后本地抽取的结果,保证新鲜度和版权/授权链路清晰。

## 项目现状

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| URL 发现 | ✅ 已验证 | sitemap(含 sitemap index)、RSS/Atom feed、页面内链 BFS、robots.txt 合规 |
| 正文抽取 | ✅ 已验证 | `trafilatura`,不可用时降级内置 HTMLParser;exact content hash 去重 |
| 三档抓取引擎 | ✅ 已验证可跑通 | turbo(同步线程池)/ async(单进程 asyncio)/ multiproc-async(多进程横向扩展),见下表 |
| 反爬检测 + TLS 指纹伪装 | ✅ 已验证 | 429/5xx 重试、验证码/WAF 挑战页识别、`curl_cffi` JA3/JA4 浏览器指纹伪装,已用 mock server 三场景 + 真实 `tls.browserleaks.com` JA4 校验通过 |
| Common Crawl 集成 | ✅ 已验证 | WET 站点发现 + CDX index 按域名导出源头去重 URL 种子 |
| HTTP 条件请求缓存 | ✅ 已验证 | ETag/Last-Modified 304 增量重访 |
| 大规模多站点真实吞吐 | ⚠️ 待压测 | 单机/单站点已验证功能正确性,尚未在大规模真实站点集上重新测过 async/multiproc 引擎的稳定吞吐 |
| 跨机器分布式部署 | 📝 设计阶段 | `PLAN_200B.md` 是面向百亿级页面的分片方案设计,尚未实际跑通多机器部署 |

图例:✅ 已验证可用 · ⚠️ 功能可用但规模化数据待补 · 📝 仅有设计文档,未实现/未跑通。

细节和已知问题清单见 `PIPELINE_ANALYSIS.md`(逐次迭代记录,含每次修复前后的对比验证)。

## 三档抓取引擎

| 引擎 | 文件 | 模型 | 适合场景 |
| --- | --- | --- | --- |
| Turbo(同步) | `src/pipeline_domain_crawler.py` + `src/domain_link_crawler.py` | 线程池 fetcher + 进程池 extractor | 单站深度全量覆盖,调试友好 |
| Async(单进程) | `src/async_pipeline_crawler.py` | 单进程 asyncio,全局连接池 + per-host 限流,多站点交错调度 | 单机大规模并发多站抓取;代码里设计目标 2048+ 全局并发 / 1500+ pages/s |
| Multiproc-Async | `src/multiproc_async_crawler.py` | N 个进程,每个进程内跑一套 Async 引擎处理一部分站点 | 单机多核横向扩展 Async 引擎;设计目标 N × 单进程速率 |

三档引擎共享同一套 URL 发现/去重/正文抽取/反爬逻辑,区别只在调度模型和并发实现,可以按机器规格和目标站点数选择。`PLAN_200B.md` 有面向百亿级页面的分片部署方案。

## 反爬检测与浏览器指纹伪装

三档引擎都具备:

- **失败重试**:429 / 5xx / 超时 / 连接错误最多重试 3 次,遵守服务端 `Retry-After`。
- **验证码/WAF 挑战页识别**:命中 Cloudflare "Just a moment"、`cf-chl-`、`captcha` 等特征直接判失败,不会被当成正文吃进库。
- **`likely_blocked` + `error_reasons`**:每个站点的 `summary.json` 会标记是否疑似被永久拦截,以及失败原因分布,和"这个站本来页面就少"区分开。
- **TLS/JA3/JA4 浏览器指纹伪装**(`src/browser_fingerprint.py` + [`curl_cffi`](https://github.com/lexiforest/curl_cffi)):按域名稳定映射到 `chrome136`/`firefox135`/`safari184`/`edge101` 等真实浏览器 impersonate profile,同时自动带上匹配的 UA / `sec-ch-ua` / `Accept-Language`,比裸 `urllib`/`aiohttp`(默认走 Python `ssl` 指纹,和真实浏览器在 TLS 握手层就能被区分)更接近真实浏览器流量。`curl_cffi` 未安装时自动降级为按域名轮换真实浏览器 UA 字符串的 `urllib`/`aiohttp` 路径,不影响功能。
- Async/Multiproc 引擎新增 `--impersonate` / `--no-impersonate`(默认开启),可强制关闭指纹伪装退回裸连接。

已用本地 anti-bot mock server(永久 429、429 重试后恢复、Cloudflare 假挑战页三种场景)和真实 HTTPS 站点(`tls.browserleaks.com` JA4 校验、`example.com` 端到端抽取)验证。详见 `PIPELINE_ANALYSIS.md` 第 9 节。

## Common Crawl 集成

Common Crawl 只用来发现"抓哪些站/哪些 URL",不作为正文来源:

- `src/common_crawl_site_discovery.py`:批量下载解析 WET 文件,从 `WARC-Target-URI` 提取候选站点,支持英文域名过滤、垂直采样(`--spread-wet-paths`)、`--processed-wets-file` 跨 run 去重(已消费的 WET 不会重复下载解析)。
- `src/cc_index_url_seeder.py`:按域名查询 CDX API(`index.commoncrawl.org`),导出该域名下的 `url + digest + mime + status` 列表;同 content digest 的 URL 组只导出一条,做**源头去重**;`--processed-domains-file` 跨 run 登记已导出域名。导出结果可通过 `--seed-urls-file` 直接灌入 turbo/async 引擎的 frontier,深层孤岛页(无内链、不在 sitemap)不必再靠 live BFS 重新发现。
- **HTTP 条件请求缓存**(`--http-cache-file`,turbo 引擎):持久化每个 URL 的 `ETag`/`Last-Modified`,重访命中 304 时不重新下载 body、不重复抽取正文,计入 `not_modified_pages`。适合"每天重访同一批站点"场景。

## 当前能力

- 域名级内部链接递归发现
- 通用 sitemap 发现 + sitemap index 递归解析
- robots.txt 合规检查
- RSS/Atom feed URL 发现
- 页面内链 BFS 扩展
- URL 规范化 + URL hash 去重
- 二进制/静态资源过滤
- trafilatura 开源正文抽取(不可用时降级到内置 HTMLParser)
- exact content hash 去重
- 失败重试 + 验证码/WAF 检测 + `likely_blocked` 标记
- TLS/JA3 浏览器指纹伪装,`curl_cffi` 缺失时自动降级
- HTTP 条件请求缓存(304 增量重访)
- Common Crawl CDX index 源头 URL 去重种子
- 并发抓取(线程池 / asyncio / 多进程 asyncio 三档可选)
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

`requirements.txt` 包含:

- `trafilatura` — 正文抽取(缺失时降级到内置 HTMLParser)
- `aiohttp` — Async/Multiproc 引擎的异步 HTTP 客户端
- `curl_cffi` — TLS/JA3 浏览器指纹伪装(缺失时自动降级为 `urllib`/`aiohttp` 裸连接,不影响功能)

## 快速运行

域名级递归抓取：

```bash
bash scripts/run_domain_docs_python_small.sh
```

高吞吐主从流水线抓取(turbo,同步)：

```bash
bash scripts/run_pipeline_docs_python_fast.sh
```

单进程异步引擎抓取(Async)：

```bash
bash scripts/run_async_crawl_10x.sh cc_seed_sites_100x_en.tsv data/runs/cc_async_10x 20000 100000 2048 512
```

多进程异步引擎抓取(Multiproc-Async，单机多核横向扩展)：

```bash
bash scripts/run_multiproc_10x.sh cc_seed_sites_100x_en.tsv data/runs/cc_multiproc_10x 8 20000 100000
```

以上两个脚本默认开启 `curl_cffi` TLS 指纹伪装；两个脚本固定位置参数之后的额外参数会原样转发给底层 Python 命令，如需关闭指纹伪装：

```bash
bash scripts/run_async_crawl_10x.sh cc_seed_sites_100x_en.tsv data/runs/cc_async_10x 20000 100000 2048 512 --no-impersonate
bash scripts/run_multiproc_10x.sh cc_seed_sites_100x_en.tsv data/runs/cc_multiproc_10x 8 20000 100000 --no-impersonate
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
