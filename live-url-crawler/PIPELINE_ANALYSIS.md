# 全链路现状分析:CC 站点发现 → 全站 Live 抓取

分析时间:2026-07-18。基于当前代码实际实现,不基于文档描述。

## 1. 链路总览

```text
Common Crawl WET 头
  └─ src/common_crawl_site_discovery.py
       输入: wet.paths.gz (按 --spread-wet-paths 均匀采样)
       解析: WARC-Target-URI → host → 过滤(IP/无字母/spam词/英文TLD)
       去重: 单 run 内存 seen_hosts (host 精确匹配)
       输出: cc_sites.jsonl / cc_seed_sites.tsv / manifest / summary

多站点并行调度
  └─ scripts/run_pipeline_multi_seed_turbo.sh
       每行 seed 一个独立进程: --allowed-domain <scope> --include-subdomains
       跳过规则: summary.json 完成 → skip; 有 progress.json 无 summary.json → skip

单站抓取
  └─ src/pipeline_domain_crawler.py
       启动: robots.txt → sitemap 递归发现(仅启动时一次) → seed 入队
       循环: fetch(线程池) → regex 抽链/抽正文 → 出链入队
       去重: URL sha256 内存 set + 正文 sha256 内存 set(均单站、单进程)
       输出: pages.jsonl / manifest.jsonl / discovered_urls.jsonl /
             frontier_remaining.jsonl / progress.json / summary.json

监控合并
  └─ src/monitor_cc_live_run.py
       按文件 offset 增量追加各站 pages.jsonl → 根目录 partial jsonl(无任何去重)
```

完成判定:`site_crawl_complete=true` 且 `stopped_reason=frontier_exhausted`。

## 2. 发现阶段遗漏点

### 2.1 站点去重不跨 run(高)
`seen_hosts` 是内存 set,进程结束即丢。换 crawl-id 或重跑会重复产出同一批站点,下游会整站重抓。缺一个持久化 host/domain 注册表。

### 2.2 www 与裸域被当成两个站点(高)
`example.com` 与 `www.example.com` 生成两条独立 seed。下游又是 `--allowed-domain <host> --include-subdomains`,`www.example.com` 是 `example.com` 的子域,两个抓取进程的 URL 空间大面积重叠 → 同站双抓、产出重复正文。同理,任意两个同注册域的子域 seed 之间也可能互相覆盖。缺 eTLD+1(publicsuffix)归并。

### 2.3 spam 词子串误杀(中)
`ADULT_OR_SPAM_TERMS` 用 `term in host` 判断:`bet` 命中 `bethesda.com`、`sex` 命中 `essex.gov.uk`、`loan` 命中 `sloane*.com`。应改为按 label 分词或词边界匹配。

### 2.4 失败 WET 不补位(低)
WET 503/超时 3 次后只记 manifest,不会取新的 WET path 补足,实际站点数系统性低于 `--max-sites`。

### 2.5 其他
- seed 协议取自单条 sample URL 的 scheme,不做存活探测;死域/停靠域浪费下游进程。
- 英文过滤只看 TLD,`.com` 上大量非英文站会漏进来;可加 host 语言启发或首页语言探测。
- 未利用 CC 的 columnar index / host 级 vertices 数据集,逐条解析 WET 头做发现效率偏低。

## 3. 抓取阶段遗漏点

### 3.1 中途发现的 sitemap / feed 是死路(高,最大覆盖漏洞)
pipeline 模式下 `fetch_worker` 把 `sitemap_link` / `feed` 出链当普通页面抓取,XML 响应被
`status != 200 or "html" not in content_type` 直接判为 `non_html_or_non_200` 丢弃。
即:**只有启动时 `discover_sitemaps` 那一次会解析 sitemap;抓取过程中新发现的子 sitemap、feed 条目永远不会展开**。
`domain_link_crawler.py`(慢速版)对 `feed`/`sitemap_link` 有专门的 `fetch_feed_urls` / `fetch_sitemap_page_urls` 分支,pipeline 版未继承。对靠 sitemap 组织、内链稀疏的站点(新闻站、电商)覆盖损失严重。

### 3.2 崩溃站点永久缺失(高)
frontier/seen 全内存,输出文件 `open("w")`。进程崩溃后该站目录残留 `progress.json` 而无 `summary.json`,`run_pipeline_multi_seed_turbo.sh` 判为 `skip_active_or_incomplete` 永久跳过。除非 `FORCE_RECRAWL=1` 整站从零重抓。缺:
- frontier / seen 持久化与 resume(`frontier_remaining.jsonl` 写了但没有任何消费入口)
- 输出追加模式 + 状态标记(如 pid 心跳)区分"运行中"与"崩溃"

### 3.3 失败 URL 无二次回捞(中)
`fetch_bytes` 内部重试 2 次后失败即写 manifest 完事。瞬时 429/5xx/超时的 URL 不回队列,也没有离线"失败重抓"工具。`stopped_reason=frontier_exhausted` 实际含义是"队列空",不等于"全部成功"。

### 3.4 redirect / canonical 不参与去重(中)
- `seen` 只记入队时的 URL hash;A、B 两个 URL 都 301 到 C 时会抓两次(正文 hash 能挡住重复落盘,但浪费抓取)。`final_url` 应回写 seen。
- `canonical` 在 `regex_extract_page_links` 里恒为 `""`(regex 模式没实现),htmlparser 模式提取了也只写 manifest,从不用于合并。

### 3.5 JS 渲染站点近乎空产出(中)
无 headless 兜底,SPA 站点正文长度趋近 0。建议至少按 `text_length < 阈值 且 html 含 root 挂载点` 打标,统计损失面,再决定是否上渲染通道。

### 3.6 无爬虫陷阱防护(中)
日历翻页、faceted 组合、session 路径参数只靠 `max-depth 50` / `max-discovered 2M` 兜底。单站可能把预算烧在无限 URL 空间。建议:URL 长度上限、query 参数个数上限、per-path-pattern 计数熔断。

### 3.7 一次性快照,无增量能力(中)
不记录/不使用 ETag、Last-Modified、sitemap `lastmod`(解析了但丢弃)。无法做"定期增量只抓新页"。

### 3.8 其他
- `max_pages` 按 scheduled 计数,非 HTML/失败请求也消耗页配额。
- politeness 仅 host 信号量,不读 robots `Crawl-delay`;单机 128 站 × 32 fetch worker 对小站压力可控,但同 host 多 seed 重叠时(见 2.2)会叠加。
- `.pdf` 被 STATIC_EXTENSIONS 一刀切过滤,若目标是 AI 语料,PDF 可能是有价值损失。
- `robots-check-stage fetch` 模式下 robots 拒绝的 URL 已写入 discovered_urls 计入 max_discovered。
- normalize 丢弃 `ref`/`source`/`from` 等 query key,个别站点用它们承载真实内容参数,会造成误合并(覆盖损失),建议按站点可配置。

## 4. 去重现状与优化

### 4.1 现状矩阵

| 层级 | 机制 | 范围 | 问题 |
|---|---|---|---|
| 站点 | `seen_hosts` host 精确匹配 | 单次 discovery 进程 | 不跨 run;无 www/eTLD+1 归并 |
| URL | `sha256 hex` 字符串内存 set | 单站单进程 | 不持久;hex+set 开销大(2M URL 约数百 MB);跨站不去重 |
| 内容 | 空白归一化后 `sha256` 精确匹配 | 单站单进程 | 无近重复检测;regex-inline 正文含 nav/footer 噪声,同文异模板判不出重 |
| 全局合并 | `cat` / offset 追加 | 无 | 最终 JSONL 完全不去重,跨站重复全保留 |

### 4.2 优化建议(按性价比)

1. **eTLD+1 归并 + www 归一(发现阶段)**
   引入 publicsuffix 列表,seed 按注册域唯一化,`www.` 前缀统一剥离。直接消除最大跨站重复源,同时减少无效并行进程。
2. **全局合并阶段内容去重**
   `monitor_cc_live_run.py` 合并时按行解析 `content_hash` 维护全局 set(或改为离线一遍 `sort -u`/sqlite),把最终 JSONL 变成真正 unique。改动小、收益直接。
3. **URL seen 集内存与持久化**
   - 内存:`sha256(url).digest()[:8]` 转 int 存 set,内存降约 10x;规模再大用 Bloom filter(可接受极低误杀)。
   - 持久:seen + frontier 落 sqlite/RocksDB,同时解决 3.2 的断点续传。
4. **final_url / canonical 回写 seen**
   抓完把 `final_url`(以及非空 canonical)的 hash 加入 seen,消除 redirect 族重复抓取。
5. **近重复去重(离线)**
   基于 pages.jsonl 已有字段离线跑 SimHash(64-bit,汉明距 ≤3 判重),不侵入抓取热路径。要更准可对候选对再做 MinHash/编辑距离复核。
6. **跨 run 站点注册表**
   一个简单的 `crawled_domains.sqlite`(domain → 状态/完成时间/页数),discovery 与调度共同读写,支撑多批次滚动放量。

## 5. 建议落地顺序

| 优先级 | 事项 | 对应问题 |
|---|---|---|
| P0 | pipeline 模式支持 sitemap/feed 出链解析 | 3.1 覆盖漏洞 |
| P0 | 发现阶段 eTLD+1 + www 归并 | 2.2 / 去重 |
| P0 | 崩溃站点可识别可续抓(frontier 持久化 + resume) | 3.2 |
| P1 | 全局合并内容去重 | 4.2-2 |
| P1 | 失败 URL 清单 + 离线回捞工具 | 3.3 |
| P1 | final_url/canonical 参与去重 | 3.4 |
| P2 | spam 词按 label 匹配;seed 存活预检 | 2.3 / 2.5 |
| P2 | 陷阱防护(URL 长度/参数数/pattern 熔断) | 3.6 |
| P2 | SimHash 近重去重离线管道 | 4.2-5 |
| P3 | 增量抓取(lastmod/ETag);JS 渲染兜底 | 3.7 / 3.5 |

## 6. 一句话结论

当前链路能跑通"CC 发现 → 并行全站抓取 → 合并",但存在两类系统性损失:**覆盖侧**(运行中 sitemap/feed 死路、崩溃站点永久跳过、失败无回捞)和**重复侧**(www/子域重叠双抓、最终合并零去重、无近重检测)。P0 三项修完后,"frontier_exhausted = 全站抓完"的判定和最终 JSONL 的唯一性才真正成立。

## 7. P0 修复记录(已实施)

### 7.1 sitemap/feed 出链展开(对应 3.1)
`src/pipeline_domain_crawler.py` 新增 `fetch_sitemap_worker` / `fetch_feed_worker`,`fetch_worker` 按 `item["source"]` 分发:
- `source == "sitemap_link"` → 按 XML 解析,子 sitemap 继续以 `sitemap_link` 出链入队,页面以 `sitemap_link_page` 出链入队(走正常页面抓取路径)。
- `source == "feed"` → 按 feed XML 解析,条目以 `feed_item` 出链入队。

抓取过程中新发现的 sitemap/feed 不再被 `non_html_or_non_200` 丢弃。

### 7.2 eTLD+1 + www 归并、跨 run 站点注册表(对应 2.2 / 2.3 / 2.1)
`src/common_crawl_site_discovery.py`:
- 新增 `registrable_domain()`,基于内置多段公共后缀表(`co.uk`/`com.au` 等)做近似 eTLD+1 归并;`site_from_target_url` 用归并后的 `registrable_domain` 作为 `scope`(而不是原始 host),下游 `--allowed-domain <scope> --include-subdomains` 天然覆盖 www 和其他子域,一个站点只产生一条 seed。
- 去重键从 `host` 换成 `registrable_domain`。
- spam 词过滤改为按 label 精确匹配(`re.split(r"[.-]", host)` 后判断),修复 `bet`→`bethesda.com`、`loan`→类似域名 的子串误杀。
- 新增 `--seen-domains-file`:跨 run 持久化已发现的注册域,重复 discovery(即使换 `--crawl-id`)不会再产出同一批站点。

### 7.3 frontier/seen 持久化与断点续抓(对应 3.2)
`src/pipeline_domain_crawler.py`:
- 新增 `--resume`:从 `discovered_urls.jsonl` 重建 `seen`,从 `pages.jsonl` 重建内容去重集合,从周期性 `frontier_checkpoint.jsonl` 恢复剩余队列,从 `manifest.jsonl` 恢复累计 fetched/ok/duplicate 统计;输出文件以追加模式打开而不是清空重写。
- 主循环按 `progress-interval` 节奏原子写入 `frontier_checkpoint.jsonl`(临时文件 + `os.replace`),正常结束后删除该文件。
- 新增 `pid.lock`(写 PID,进程存活性用 `os.kill(pid, 0)` 探测),防止对同一 `output_dir` 并发误跑两个进程。
- `scripts/run_pipeline_multi_seed_turbo.sh` 相应调整:存在存活 `pid.lock` → 跳过;存在 `manifest.jsonl` 但无 `summary.json` 且无存活锁 → 传 `--resume` 续抓,而不是永久 `skip_active_or_incomplete`。

以上三项修复后,`site_crawl_complete=true` + `stopped_reason=frontier_exhausted` 才真正对应"sitemap/feed 全展开 + 进程崩溃可恢复"下的全站抓取语义。仍待办的是 P1/P2/P3(见第 5 节),尤其是全局内容去重与近重复检测。

## 8. 第二轮分析:面向「每天几十亿页、不遗漏、源头不重复」

### 8.1 新发现的系统性差距

1. **CC 的 URL 清单被丢弃(最大遗漏 + 最大浪费)**
   原链路只从 WET header 取 host 做站点发现,站内 URL 全靠 live BFS 重新找。CC 单月 crawl 自带 ~30 亿条 URL 记录且每条带内容 digest:深层页/孤岛页(无内链、不在 sitemap)BFS 永远发现不了;同时 CC 已知的 URL 还要靠爬列表页重新"发现"一遍。
2. **WET 文件级无处理登记**
   `--seen-domains-file` 只去重域名,不记录处理过哪些 WET;跨 run/崩溃重跑会重新下载解析同一批 WET(每个解压 ~150MB)。全量 9 万个文件必须有 per-file done registry。
3. **每日重访无条件请求**
   持续重抓时没有 `If-None-Match`/`If-Modified-Since`,未变更页面全量重新下载。这是"每天抓"场景下最大的源头重复。
4. **吞吐差 2-3 个数量级**
   目标几十亿/天 ≈ 23k-58k pages/s;当前同步 urllib + 线程池单机 ~158 pages/s。带宽是硬瓶颈(100KB/页 × 30k/s ≈ 24 Gbps),单机物理上不可能。

### 8.2 P0 第二批修复记录(已实施)

1. **CC index URL 级种子(对应 8.1-1)**
   - 新增 `src/cc_index_url_seeder.py`:按域名查询 CDX API(`index.commoncrawl.org`),导出 `url + digest + mime + status` JSONL。
   - **digest 源头去重**:同 content digest 的 URL 组只导出第一个(example.com 的 http/https/www 5 个变体 → 1 条);URL 经统一 `normalize_url` 归一(utm 等 tracking 参数剥离)再按 hash 去重;`--html-only` 过滤非 HTML。
   - `--processed-domains-file` 跨 run 登记,已导出域名不重复查询。
   - `src/pipeline_domain_crawler.py` 新增 `--seed-urls-file`:URL 清单直接灌入 frontier(source=`cc_index_seed`),BFS 降级为增量补充;`--resume` 时自动跳过(已在 discovered 中)。
   - `run_pipeline_multi_seed_turbo.sh` 新增 `SEED_URLS_DIR` 环境变量,存在 `$SEED_URLS_DIR/<output_name>.jsonl` 时自动挂载。
2. **WET 处理登记(对应 8.1-2)**
   `src/common_crawl_site_discovery.py` 新增 `--processed-wets-file`:完整消费的 WET 路径登记后跨 run 跳过;被 `--max-sites` 截断或下载失败的文件不登记、下次重试。已验证:第二次运行 `wet_paths_skipped_already_processed=2, wet_paths_considered=0`,零重复下载。
3. **ETag/Last-Modified 条件请求(对应 8.1-3)**
   - `domain_link_crawler.fetch_bytes` 支持 `extra_headers`,HTTP 304 不重试直接上抛。
   - `src/pipeline_domain_crawler.py` 新增 `--http-cache-file`:JSONL 持久化 `url_hash → etag/last_modified`,重访自动带条件头;304 计入 `not_modified_pages`,不重复下载 body、不重复抽取。
   - `run_pipeline_multi_seed_turbo.sh` 新增 `HTTP_CACHE_DIR` 环境变量。
   - **每日重访模式**:上一轮 `discovered_urls.jsonl` 作为下一轮 `--seed-urls-file` + 共享 http cache。已验证:重访 3 URL 全部 304,零重复抽取。

### 8.3 几十亿/天的架构路线(待实施)

| 层 | 现状 | 目标 |
|---|---|---|
| URL 供给 | WET 站点发现 + CDX 种子(本轮已加) | CC columnar index(parquet + DuckDB/Athena)批量导出,快几个数量级 |
| 调度分片 | 单机多进程 | 按 registrable_domain hash 分片到 10-15 台机器:politeness 天然隔离、机器间零重复、无需全局协调 |
| 抓取引擎 | 同步 urllib + 线程(~158/s) | `async_pipeline_crawler.py`(aiohttp,已有,未接入主链路)+ uvloop + aiodns,单机 2-5k/s |
| seen/frontier | Python set + JSONL 重建 | Bloom filter(10 亿 URL @0.1% ≈ 1.7GB)或 RocksDB |
| 内容去重 | SHA1 精确(per-site) | CC digest 抓前去重(已加)+ SimHash 近重复(`optimized_live_crawler.py` 已有实现,待移植) |
| 存储 | per-site JSONL 目录 | WARC.gz / parquet 滚动分片 |

粗算:30 亿/天 ≈ 35k pages/s ≈ 15 台 × 2.3k/s;出口带宽需 20-30 Gbps。
