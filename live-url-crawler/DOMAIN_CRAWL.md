# Domain Crawl Workflow

目标：拿到一个域名后，尽可能发现并抓取该域名下所有可访问 HTML 页面，抽取文字内容。

## 流程

```text
seed_url
  -> robots.txt
  -> sitemap URLs
  -> common sitemap fallback
  -> sitemap page URLs 入队
  -> seed page 入队
  -> feed URLs 入队
  -> BFS 抓取页面
  -> 从页面继续抽取内部链接
  -> URL 去重
  -> robots 检查
  -> HTML 抓取
  -> 请求重试 / backoff
  -> 页面内 feed/sitemap link 继续解析
  -> trafilatura 正文抽取
  -> 内容 hash 去重
  -> 输出 discovered/manifest/extracted/frontier
```

## 小规模验证

```bash
bash scripts/run_domain_docs_python_small.sh
```

## 任意域名运行

```bash
bash scripts/run_domain_from_seed.sh https://example.com/ data/runs/example_com example.com
```

如果只抓 seed host，不传第三个参数：

```bash
bash scripts/run_domain_from_seed.sh https://docs.python.org/3/ data/runs/docs_python_org
```

## 覆盖边界

“所有链接”在工程上指：

```text
在 robots 允许范围内，
从 sitemap、feed、seed page、页面内链递归发现到的，
经过 URL 规范化和去重后的，
同域名/同 host HTML URL。
```

不包括：

```text
robots 禁止的 URL
需要登录的 URL
JS 执行后才出现的 URL
表单提交生成的 URL
被静态/二进制扩展过滤的 URL
超过 max-pages/max-depth/max-discovered 限制后的剩余 frontier
```

如果要继续扩大覆盖，增大：

```text
--max-pages
--max-discovered
--max-depth
--max-sitemaps
```

并检查 `frontier_remaining.jsonl`。

## 开源项目借鉴

当前实现参考方向：

- `Scrapy SitemapSpider`：sitemap / sitemap index 递归
- `Scrapy LinkExtractor`：页面链接抽取和 URL 规范化思路
- `Scrapy RetryMiddleware`：失败重试
- `Crawlee RequestQueue`：后续持久化 frontier 的方向
- `Heritrix / Nutch Frontier`：host-level queue 和 politeness 的方向
- `trafilatura`：正文抽取

详细说明见 `OPEN_SOURCE_REFERENCES.md`。
