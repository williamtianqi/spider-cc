# Coverage Notes

## 当前样例不是无遗漏全站抓取

本项目当前 sample run 是参数限制下的真实站点抓取验证，不代表 `docs.python.org` 的完整覆盖。

当前样例：

```text
base_url: https://docs.python.org/3/
include_path_prefix: /3/
candidates: 549
requested_pages: 80
unique_downloaded_pages: 80
```

结果文件：

```text
data/sample_run/docs_python_org/summary.json
data/sample_run/docs_python_org/candidates.csv
data/sample_run/docs_python_org/manifest.jsonl
data/sample_run/docs_python_org/extracted_text.jsonl
```

## 如何接近无遗漏

单机 demo 层面需要：

```text
--max-sitemaps 覆盖全部 sitemap index
--max-candidates 大于发现候选 URL 总量
--max-pages 大于候选 URL 总量
--link-discovery-pages 增大一跳链接发现范围
```

生产层面还需要：

- 持久化 URL Frontier
- 每个 host 的 politeness 控制
- robots 缓存
- sitemap ETag / Last-Modified 增量
- 抓取失败重试和 backoff
- canonical URL 合并
- redirect 合并
- 内容 hash 去重
- SimHash / MinHash / embedding 二级去重
- 每站 URL pattern 白名单和黑名单
- 监控 rejected URL 和 crawl miss

## 当前已验证覆盖

当前样例已经验证：

- robots.txt 可访问
- sitemap.xml 可发现并解析
- 首页链接可发现
- 一跳链接可扩展
- URL hash 去重有效
- 二进制资源可提前过滤
- trafilatura 正文抽取可用
- exact content hash 去重可用
- SimHash 近重复去重可用
