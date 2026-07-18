# Open Source References

本项目当前目标是：拿到域名后，尽可能发现该域名下所有可抓 HTML URL，并抽取文字内容。

以下开源项目/能力值得借鉴。

## Scrapy

适合借鉴：

- `SitemapSpider`：sitemap / sitemap index 递归处理、sitemap 规则匹配
- `LinkExtractor`：HTML 链接抽取、allow/deny 规则、canonicalize
- Scheduler / DupeFilter：请求队列和 URL 去重
- RetryMiddleware：失败重试
- AutoThrottle：自动限速
- DepthMiddleware：递归深度控制

本项目当前保留轻量自研实现，原因：

- 更容易看清 URL 发现、入队、抓取、抽取、输出全链路
- 方便后续改造成分布式 frontier
- 避免一开始被框架生命周期绑定

后续如果要工程化单机爬取，可以把 `domain_link_crawler.py` 改成 Scrapy spider。

## Crawlee

适合借鉴：

- RequestQueue：持久化请求队列
- 自动重试和失败处理
- 并发控制
- 会话管理
- 爬取状态持久化

本项目后续最该借鉴的是 `RequestQueue` 思路：

```text
url
normalized_url
source
depth
from_url
status
retry_count
next_retry_at
```

## Heritrix / Apache Nutch

适合借鉴：

- Frontier 设计
- politeness / host-level queue
- URL 规范化和 scope 控制
- robots 缓存
- 大规模 crawl state 管理

如果目标是长期大规模抓取，最终应该从当前单机 demo 过渡到：

```text
Domain Discovery
  -> Sitemap / Feed / Link Discovery
  -> URL Frontier
  -> Host-level Scheduler
  -> Fetcher Pool
  -> Parser / Extractor
  -> Deduper
  -> Storage
```

## Trafilatura

当前已使用。

适合：

- 正文提取
- 标题和 metadata 抽取
- 去 boilerplate
- 兼容新闻、博客、文档类网页

当前 `domain_link_crawler.py` 使用 `trafilatura.extract()`，不可用时 fallback 到内置 HTMLParser。

## 当前实现取舍

当前版本不是为了替代 Scrapy/Crawlee，而是为了验证核心链路：

```text
域名 -> sitemap/feed/页面内链 -> URL 去重 -> HTML 抓取 -> 正文抽取 -> 输出
```

保留自研版本的原因：

- 更容易改造成分布式
- 更容易接入你自己的 URL Frontier
- 更容易控制 URL 发现逻辑
- 更容易观察所有中间文件

## 下一步可借鉴并实现

优先级建议：

1. 持久化 RequestQueue / Frontier
2. host-level politeness queue
3. retry/backoff 持久化
4. sitemap ETag / Last-Modified 增量
5. URL pattern 级限流
6. canonical / redirect 合并
7. 分布式 fetcher
8. 内容存储和索引 pipeline
