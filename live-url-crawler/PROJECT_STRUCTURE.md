# Project Structure

```text
live-url-crawler/
  README.md
  PIPELINE_ANALYSIS.md
  COVERAGE_NOTES.md
  DOMAIN_CRAWL.md
  OPEN_SOURCE_REFERENCES.md
  PLAN_200B.md
  PROJECT_STRUCTURE.md
  CLAUDE.md
  requirements.txt

  src/
    domain_link_crawler.py        # turbo(同步)抓取核心: URL 发现/去重/正文抽取/反爬检测/curl_cffi 指纹伪装
    pipeline_domain_crawler.py    # turbo 主从流水线 CLI(fetch 线程池 + extract 进程池)
    async_pipeline_crawler.py     # 单进程 asyncio 引擎, 全局连接池 + per-host 限流
    multiproc_async_crawler.py    # N 进程 x async 引擎, 单机多核横向扩展
    browser_fingerprint.py        # curl_cffi impersonate profile 选择 + UA 轮换降级
    optimized_live_crawler.py     # 候选 URL 优先级抓取, 可选保留全文/HTML
    common_crawl_site_discovery.py # CC WET -> 候选站点/域名发现
    cc_index_url_seeder.py        # CC CDX index -> per-domain URL+digest 种子(源头去重)
    monitor_cc_live_run.py        # 大规模抓取的非阻塞监控 + partial JSONL 合并
    monitor_run.py
    check_cc_live_status.py
    analyze_crawl_quality.py
    estimate_scale.py
    verify_full_site_crawl.py

  scripts/
    run_docs_python_sample.sh
    run_domain_docs_python_small.sh
    run_domain_docs_python_fast.sh
    run_domain_from_seed.sh
    run_pipeline_docs_python_fast.sh
    run_pipeline_docs_python_turbo.sh
    run_pipeline_500k_from_seed.sh
    run_pipeline_multi_seed_turbo.sh
    run_async_crawl_10x.sh        # Async 引擎启动脚本
    run_multiproc_10x.sh          # Multiproc-Async 引擎启动脚本
    run_common_crawl_sites_100x.sh
    run_common_crawl_sites_500k.sh
    run_full_pipeline.sh
    run_extreme_speed.sh
    show_pages_sample.sh
    monitor_run.sh

  configs/
    docs_python_org.json
    domain_docs_python_small.json
    multi_seed_example.tsv

  data/
    sample_run/
      docs_python_org/
        summary.json
        candidates.csv
        manifest.jsonl
        extracted_text.jsonl

    runs/
      <runtime outputs, gitignored>
```

## Main Scripts

### `src/domain_link_crawler.py`

用于你当前目标：拿到一个域名后，尽可能发现该域名下所有可抓的内部链接，并把 HTML 正文抽取下来。

它不做内容质量评分，只做必要边界：

```text
robots 合规
域名范围控制
URL 规范化
URL 去重
静态/二进制 URL 过滤
sitemap / feed 解析
页面链接 BFS 递归
请求重试 / backoff, 遵守 Retry-After
验证码/WAF 挑战页检测 (likely_blocked)
curl_cffi TLS/JA3 浏览器指纹伪装 (缺失时降级 urllib + 轮换 UA)
正文抽取
内容 hash 去重
HTTP 条件请求缓存 (ETag/Last-Modified, --http-cache-file)
```

### `src/pipeline_domain_crawler.py`

高吞吐单机主从流水线版本 (turbo)。

```text
master/frontier
  -> fetcher thread pool
  -> extractor process pool
  -> writer JSONL
```

主要输出正文文件是 `pages.jsonl`。支持 `--seed-urls-file` 直接灌入 `cc_index_url_seeder.py` 导出的 URL 清单,支持 `--resume` 从中断处续抓。

### `src/async_pipeline_crawler.py`

单进程 asyncio 引擎。全局连接池(`--max-concurrent`)+ per-host 限流(`--max-per-host`)+ 多站点交错调度,取代 turbo 版的线程池模型。默认通过 `--impersonate`(可用 `--no-impersonate` 关闭)启用 `curl_cffi` TLS 指纹伪装。反爬检测/重试逻辑与 turbo 版一致。

### `src/multiproc_async_crawler.py`

N 个进程,每个进程内跑一套 `AsyncCrawlEngine` 处理一部分站点(按站点 round-robin 分片),用于单机多核横向扩展 Async 引擎;各 worker 独立输出 `*.workerN` 后缀的 stats/partial/latest 文件,最终合并。

### `src/browser_fingerprint.py`

按域名稳定选择 `curl_cffi impersonate` profile(chrome/firefox/safari/edge),启动时用安装版本自带的 `BrowserType` 枚举过滤,只保留真正可用的 profile(不同 `curl_cffi` 版本支持的 profile 集合不同)。`curl_cffi` 未安装时提供按域名轮换的真实浏览器 UA 字符串作为降级方案。

### `src/optimized_live_crawler.py`

用于高质量候选 URL 筛选和优先级抓取。这个更适合后续做质量评分、URL pattern 策略、优先级调度。

### `src/common_crawl_site_discovery.py` / `src/cc_index_url_seeder.py`

Common Crawl 集成:前者从 WET 文件的 `WARC-Target-URI` 发现候选站点/域名(带 `--processed-wets-file` 跨 run 去重);后者按域名查询 CDX index 导出 `url+digest+mime+status` 列表并按 content digest 去重,导出结果可作为 `--seed-urls-file` 直接灌入 turbo/async 引擎。

## Domain-wide Output

`domain_link_crawler.py` 输出：

```text
summary.json
抓取汇总; 新增 likely_blocked(疑似被反爬永久拦截)、error_reasons(失败原因分布)、
not_modified_pages(HTTP 304 计数, 需配合 --http-cache-file)

discovered_urls.csv
所有已发现并入队过的 URL

links.csv
页面链接边，from_url -> to_url

manifest.jsonl
每次抓取请求状态

pages.jsonl
pipeline 模式正文抽取结果，每行一个页面 JSON

extracted_text.jsonl
普通模式正文抽取结果

sitemap_reports.jsonl
sitemap 探测和解析结果

frontier_remaining.jsonl
达到 max-pages 或 max-discovered 后剩余未抓 frontier

progress.json
运行中的实时进度快照

crawl.log
周期性抓取日志

quality_report.json
抓取完成后的质量分析报告
```
