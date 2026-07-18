# Project Structure

```text
live-url-crawler/
  README.md
  COVERAGE_NOTES.md
  DOMAIN_CRAWL.md
  OPEN_SOURCE_REFERENCES.md
  PROJECT_STRUCTURE.md
  requirements.txt

  src/
    optimized_live_crawler.py
    domain_link_crawler.py
    pipeline_domain_crawler.py
    analyze_crawl_quality.py
    monitor_run.py

  scripts/
    run_docs_python_sample.sh
    run_domain_docs_python_small.sh
    run_domain_docs_python_fast.sh
    run_pipeline_docs_python_fast.sh
    run_domain_from_seed.sh
    show_pages_sample.sh
    monitor_run.sh

  configs/
    docs_python_org.json
    domain_docs_python_small.json

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
请求重试 / backoff
正文抽取
内容 hash 去重
```

### `src/pipeline_domain_crawler.py`

高吞吐单机主从流水线版本。

```text
master/frontier
  -> fetcher thread pool
  -> extractor process pool
  -> writer JSONL
```

主要输出正文文件是 `pages.jsonl`。

### `src/optimized_live_crawler.py`

用于高质量候选 URL 筛选和优先级抓取。这个更适合后续做质量评分、URL pattern 策略、优先级调度。

## Domain-wide Output

`domain_link_crawler.py` 输出：

```text
summary.json
抓取汇总

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
