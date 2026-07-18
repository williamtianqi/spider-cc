#!/usr/bin/env python3
"""
高吞吐异步 pipeline crawler。目标: 单机 10x-100x 吞吐。

架构:
  - aiohttp 异步 HTTP (替换 urllib 同步 IO)
  - 全局连接池 + per-host 并发限制
  - 异步 frontier + 多站点交错调度
  - 零拷贝 regex 正文抽取
  - 异步 JSONL 写入 (buffered)

对比 pipeline_domain_crawler.py:
  - 旧: 128 并行站点进程, 每站 32 线程 urllib, ~158 pages/s
  - 新: 单进程 asyncio, 全局 2048+ 并发连接, 目标 1500+ pages/s
"""
import argparse
import asyncio
import gzip
import hashlib
import html as html_module
import json
import re
import time
from collections import Counter, deque
from pathlib import Path
from urllib.parse import quote, urljoin, urldefrag, urlparse, urlunparse, parse_qsl, urlencode

try:
    import aiohttp
except ImportError:
    raise SystemExit("需要 aiohttp: pip install aiohttp")

import browser_fingerprint as fp

try:
    from curl_cffi.requests import AsyncSession as CffiAsyncSession
    from curl_cffi.requests import RequestsError as CffiRequestsError
except ImportError:
    CffiAsyncSession = None
    CffiRequestsError = Exception

USER_AGENT = "live-url-crawler/2.0 (async)"
BLOCKING_ERROR_REASONS = {"403", "429", "503", "challenge_page"}
CHALLENGE_MARKERS = (
    "checking your browser",
    "just a moment",
    "cf-browser-verification",
    "cf-chl-",
    "verify you are human",
    "enable javascript and cookies",
    "attention required! | cloudflare",
    "captcha",
    "access denied",
)
STATIC_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".rar", ".7z", ".pdf", ".epub", ".mp3", ".mp4", ".avi", ".mov",
    ".wmv", ".woff", ".woff2", ".ttf", ".eot", ".apk", ".dmg", ".exe",
}
DROP_QUERY_PREFIXES = ("utm_", "gad_")
DROP_QUERY_KEYS = {
    "fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "sessionid", "sid",
    "spm", "ref", "source", "from", "__hsfp", "__hssc", "__hstc",
    "gbraid", "wbraid", "msclkid", "trk", "trkcampaign",
}

HREF_RE = re.compile(r'''(?i)\bhref\s*=\s*["']([^"'#\s>]{1,2000})["']''')
TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript|svg|canvas|template)[^>]*>.*?</\1>")
TAG_RE = re.compile(r"(?s)<[^>]+>")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_RE = re.compile(r"\n\s*\n+")
CHARSET_RE = re.compile(r'''(?i)charset\s*=\s*["']?([A-Za-z0-9._-]+)''')


def normalize_url(raw_url, base_url=None):
    if not raw_url:
        return None
    if base_url:
        raw_url = urljoin(base_url, raw_url)
    raw_url, _ = urldefrag(raw_url.strip())
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.hostname.lower() if parsed.hostname else ""
    if not host:
        return None
    netloc = host
    if parsed.port and not ((parsed.scheme == "http" and parsed.port == 80) or (parsed.scheme == "https" and parsed.port == 443)):
        netloc = f"{host}:{parsed.port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if any(path.lower().endswith(ext) for ext in STATIC_EXTENSIONS):
        return None
    path = quote(path, safe="/%:@")
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if any(key.startswith(prefix) for prefix in DROP_QUERY_PREFIXES):
            continue
        if key.lower() in DROP_QUERY_KEYS:
            continue
        query_pairs.append((key, value))
    query = urlencode(query_pairs, doseq=False)
    return urlunparse((parsed.scheme, netloc, path, "", query, ""))


def url_hash(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


def content_hash(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def host_of(url):
    try:
        return urlparse(url).hostname.lower()
    except Exception:
        return ""


def in_scope(url, allowed_domains, include_subdomains):
    host = host_of(url)
    if not host:
        return False
    for domain in allowed_domains:
        if host == domain:
            return True
        if include_subdomains and host.endswith("." + domain):
            return True
    return False


def decode_html_bytes(data, content_type=""):
    candidates = []
    header_match = CHARSET_RE.search(content_type)
    if header_match:
        candidates.append(header_match.group(1))
    head = data[:4096].decode("ascii", errors="ignore")
    meta_match = CHARSET_RE.search(head)
    if meta_match:
        candidates.append(meta_match.group(1))
    candidates.extend(["utf-8", "latin-1"])
    for charset in candidates:
        try:
            return data.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def extract_content(html):
    title_match = TITLE_RE.search(html)
    title = html_module.unescape(TAG_RE.sub(" ", title_match.group(1))).strip() if title_match else ""
    body = SCRIPT_STYLE_RE.sub(" ", html)
    body = re.sub(r"(?i)</(p|div|section|article|li|tr|h[1-6]|br)>\s*", "\n", body)
    text = TAG_RE.sub(" ", body)
    text = html_module.unescape(text)
    text = SPACE_RE.sub(" ", text)
    text = BLANK_RE.sub("\n", text).strip()
    return title, text


def extract_links(html, base_url, allowed_domains, include_subdomains):
    links = []
    for href in HREF_RE.findall(html):
        normalized = normalize_url(href, base_url)
        if not normalized:
            continue
        if not in_scope(normalized, allowed_domains, include_subdomains):
            continue
        lower = normalized.lower()
        if "/_static/" in lower:
            continue
        if lower.endswith(".xml") and "sitemap" not in lower:
            continue
        links.append(normalized)
    return links


class SiteCrawler:
    """单站点爬取状态"""

    def __init__(self, seed_url, scope_domain, output_dir, max_pages, max_discovered, max_depth):
        self.seed_url = seed_url
        self.scope_domain = scope_domain
        self.allowed_domains = {scope_domain}
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_pages = max_pages
        self.max_discovered = max_discovered
        self.max_depth = max_depth
        self.queue = deque()
        self.seen = set()
        self.content_hashes = set()
        self.scheduled = 0
        self.fetched = 0
        self.ok_pages = 0
        self.unique_text = 0
        self.discovered = 0
        self.in_flight = 0
        self.started = time.time()
        self.pages_handle = (self.output_dir / "pages.jsonl").open("w", encoding="utf-8")
        self.finished = False
        self.stopped_reason = ""
        self.error_reasons = Counter()

        normalized = normalize_url(seed_url)
        if normalized:
            digest = url_hash(normalized)
            self.seen.add(digest)
            self.queue.append({"url": normalized, "depth": 0, "url_hash": digest})
            self.discovered = 1

    def enqueue(self, url, depth):
        if self.discovered >= self.max_discovered:
            return
        normalized = normalize_url(url)
        if not normalized:
            return
        if not in_scope(normalized, self.allowed_domains, True):
            return
        digest = url_hash(normalized)
        if digest in self.seen:
            return
        self.seen.add(digest)
        self.queue.append({"url": normalized, "depth": depth, "url_hash": digest})
        self.discovered += 1

    def next_item(self):
        while self.queue:
            if self.scheduled >= self.max_pages:
                self.stopped_reason = "max_pages_reached"
                return None
            item = self.queue.popleft()
            if item["depth"] > self.max_depth:
                continue
            self.scheduled += 1
            self.in_flight += 1
            return item
        return None

    def record_result(self, url, title, text, depth):
        self.fetched += 1
        self.in_flight -= 1
        self.ok_pages += 1
        c_hash = content_hash(text)
        if c_hash in self.content_hashes:
            return
        self.content_hashes.add(c_hash)
        self.unique_text += 1
        record = {
            "url": url,
            "title": title,
            "text_length": len(text),
            "content_hash": c_hash,
            "text_preview": text[:1000],
            "depth": depth,
        }
        self.pages_handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def record_fail(self, reason="unknown"):
        self.fetched += 1
        self.in_flight -= 1
        self.error_reasons[reason] += 1

    def is_done(self):
        if self.stopped_reason and self.in_flight <= 0:
            self.finished = True
            return True
        if not self.queue and self.in_flight <= 0:
            self.finished = True
            self.stopped_reason = self.stopped_reason or "frontier_exhausted"
            return True
        return False

    def finalize(self):
        self.pages_handle.flush()
        self.pages_handle.close()
        elapsed = time.time() - self.started
        site_crawl_complete = len(self.queue) == 0 and self.stopped_reason == "frontier_exhausted"
        blocking_errors = sum(v for k, v in self.error_reasons.items() if k in BLOCKING_ERROR_REASONS)
        likely_blocked = self.unique_text == 0 and self.fetched > 0 and blocking_errors >= max(1, self.fetched * 0.5)
        summary = {
            "seed_url": self.seed_url,
            "scope_domain": self.scope_domain,
            "site_crawl_complete": site_crawl_complete,
            "stopped_reason": self.stopped_reason or "frontier_exhausted",
            "discovered_urls": self.discovered,
            "scheduled_pages": self.scheduled,
            "fetched_pages": self.fetched,
            "ok_pages": self.ok_pages,
            "unique_text_pages": self.unique_text,
            "remaining_frontier": len(self.queue),
            "elapsed_seconds": round(elapsed, 2),
            "likely_blocked": likely_blocked,
            "error_reasons": dict(self.error_reasons.most_common(5)),
        }
        (self.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary


class AsyncCrawlEngine:
    """全局异步调度引擎"""

    def __init__(self, args):
        self.args = args
        self.max_concurrent = args.max_concurrent
        self.max_per_host = args.max_per_host
        self.timeout = aiohttp.ClientTimeout(total=args.timeout, connect=5)
        self.use_cffi = bool(CffiAsyncSession is not None and getattr(args, "impersonate", True))
        self.host_semaphores = {}
        self.global_semaphore = asyncio.Semaphore(self.max_concurrent)
        self.sites = []
        self.active_sites = []
        self.completed_sites = []
        self.total_fetched = 0
        self.total_unique = 0
        self.total_ok = 0
        self.started = time.time()
        self.stats_path = Path(args.stats_jsonl)
        self.partial_path = Path(args.partial_jsonl)
        self.latest_path = Path(args.latest_json)

    def get_host_semaphore(self, url):
        host = host_of(url)
        if host not in self.host_semaphores:
            self.host_semaphores[host] = asyncio.Semaphore(self.max_per_host)
        return self.host_semaphores[host]

    def load_seeds(self, seeds_tsv, max_sites, pages_per_site, max_discovered, max_depth, output_root):
        count = 0
        with open(seeds_tsv, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                seed_url, scope, output_name = parts[0], parts[1], parts[2]
                output_dir = Path(output_root) / output_name
                if (output_dir / "summary.json").exists():
                    try:
                        s = json.loads((output_dir / "summary.json").read_text())
                        if s.get("site_crawl_complete") and s.get("stopped_reason") == "frontier_exhausted":
                            continue
                    except Exception:
                        pass
                site = SiteCrawler(seed_url, scope, str(output_dir), pages_per_site, max_discovered, max_depth)
                self.sites.append(site)
                count += 1
                if count >= max_sites:
                    break
        print(f"Loaded {count} sites to crawl", flush=True)

    async def fetch_one(self, session, site, item):
        if self.use_cffi:
            return await self._fetch_one_cffi(session, site, item)
        return await self._fetch_one_aiohttp(session, site, item)

    async def _fetch_one_aiohttp(self, session, site, item):
        url = item["url"]
        depth = item["depth"]
        host = host_of(url)
        host_sem = self.get_host_semaphore(url)
        req_headers = {"User-Agent": fp.user_agent_for_host(host), "Accept": "text/html,*/*;q=0.8"}
        data = None
        content_type = ""
        final_url = url
        max_attempts = 3
        last_reason = "unknown"
        for attempt in range(max_attempts):
            async with self.global_semaphore:
                async with host_sem:
                    try:
                        async with session.get(url, headers=req_headers, allow_redirects=True, max_redirects=5) as resp:
                            if resp.status == 429 or resp.status >= 500:
                                last_reason = str(resp.status)
                                if attempt < max_attempts - 1:
                                    retry_after = resp.headers.get("Retry-After")
                                    wait_s = (attempt + 1) * 1.5
                                    if retry_after:
                                        try:
                                            wait_s = min(float(retry_after), 30.0)
                                        except ValueError:
                                            pass
                                    await asyncio.sleep(wait_s)
                                    continue
                                site.record_fail(last_reason)
                                self.total_fetched += 1
                                return
                            if resp.status != 200:
                                site.record_fail(str(resp.status))
                                self.total_fetched += 1
                                return
                            content_type = resp.headers.get("Content-Type", "")
                            if "html" not in content_type.lower():
                                site.record_fail("non_html")
                                self.total_fetched += 1
                                return
                            data = await resp.read()
                            final_url = str(resp.url)
                            if len(data) > self.args.max_page_bytes:
                                site.record_fail("too_large")
                                self.total_fetched += 1
                                return
                            break
                    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        last_reason = type(exc).__name__
                        if attempt < max_attempts - 1:
                            await asyncio.sleep((attempt + 1) * 1.5)
                            continue
                        site.record_fail(last_reason)
                        self.total_fetched += 1
                        return
                    except Exception as exc:
                        site.record_fail(repr(exc)[:40])
                        self.total_fetched += 1
                        return

        lower_snippet = data[:4096].decode("utf-8", errors="ignore").lower()
        if any(marker in lower_snippet for marker in CHALLENGE_MARKERS):
            site.record_fail("challenge_page")
            self.total_fetched += 1
            return

        html = decode_html_bytes(data, content_type)
        title, text = extract_content(html)
        if text and len(text) > 50:
            site.record_result(final_url, title, text, depth)
            self.total_unique += 1
        else:
            site.record_fail("empty_content")
            self.total_fetched += 1
            return

        links = extract_links(html, final_url, site.allowed_domains, True)
        for link in links[:500]:
            site.enqueue(link, depth + 1)
        self.total_fetched += 1

    async def _fetch_one_cffi(self, session, site, item):
        """用 curl_cffi 做浏览器 TLS/JA3 指纹伪装的抓取路径。"""
        url = item["url"]
        depth = item["depth"]
        host = host_of(url)
        host_sem = self.get_host_semaphore(url)
        profile = fp.profile_for_host(host)
        data = None
        content_type = ""
        final_url = url
        max_attempts = 3
        last_reason = "unknown"
        for attempt in range(max_attempts):
            async with self.global_semaphore:
                async with host_sem:
                    resp = None
                    try:
                        resp = await session.get(
                            url, impersonate=profile, allow_redirects=True, max_redirects=5,
                            timeout=self.args.timeout, stream=True,
                        )
                        if resp.status_code == 429 or resp.status_code >= 500:
                            last_reason = str(resp.status_code)
                            if attempt < max_attempts - 1:
                                retry_after = resp.headers.get("Retry-After")
                                wait_s = (attempt + 1) * 1.5
                                if retry_after:
                                    try:
                                        wait_s = min(float(retry_after), 30.0)
                                    except ValueError:
                                        pass
                                await resp.aclose()
                                await asyncio.sleep(wait_s)
                                continue
                            site.record_fail(last_reason)
                            self.total_fetched += 1
                            await resp.aclose()
                            return
                        if resp.status_code != 200:
                            site.record_fail(str(resp.status_code))
                            self.total_fetched += 1
                            await resp.aclose()
                            return
                        content_type = resp.headers.get("Content-Type", "")
                        if "html" not in content_type.lower():
                            site.record_fail("non_html")
                            self.total_fetched += 1
                            await resp.aclose()
                            return
                        chunks = []
                        total = 0
                        too_large = False
                        async for chunk in resp.aiter_content():
                            total += len(chunk)
                            if total > self.args.max_page_bytes:
                                too_large = True
                                break
                            chunks.append(chunk)
                        await resp.aclose()
                        if too_large:
                            site.record_fail("too_large")
                            self.total_fetched += 1
                            return
                        data = b"".join(chunks)
                        final_url = resp.url
                        break
                    except asyncio.CancelledError:
                        raise
                    except (CffiRequestsError, asyncio.TimeoutError) as exc:
                        if resp is not None:
                            try:
                                await resp.aclose()
                            except Exception:
                                pass
                        last_reason = type(exc).__name__
                        if attempt < max_attempts - 1:
                            await asyncio.sleep((attempt + 1) * 1.5)
                            continue
                        site.record_fail(last_reason)
                        self.total_fetched += 1
                        return
                    except Exception as exc:
                        if resp is not None:
                            try:
                                await resp.aclose()
                            except Exception:
                                pass
                        site.record_fail(repr(exc)[:40])
                        self.total_fetched += 1
                        return

        lower_snippet = data[:4096].decode("utf-8", errors="ignore").lower()
        if any(marker in lower_snippet for marker in CHALLENGE_MARKERS):
            site.record_fail("challenge_page")
            self.total_fetched += 1
            return

        html = decode_html_bytes(data, content_type)
        title, text = extract_content(html)
        if text and len(text) > 50:
            site.record_result(final_url, title, text, depth)
            self.total_unique += 1
        else:
            site.record_fail("empty_content")
            self.total_fetched += 1
            return

        links = extract_links(html, final_url, site.allowed_domains, True)
        for link in links[:500]:
            site.enqueue(link, depth + 1)
        self.total_fetched += 1

    def collect_items(self, max_items):
        """从活跃站点收集待抓 items，round-robin 分配"""
        items = []
        sites_done = []
        for site in self.active_sites:
            if site.is_done():
                sites_done.append(site)
                continue
            per_site = max(4, max_items // max(1, len(self.active_sites)))
            for _ in range(per_site):
                if len(items) >= max_items:
                    break
                item = site.next_item()
                if item is None:
                    break
                items.append((site, item))
        for site in sites_done:
            if site in self.active_sites:
                self.active_sites.remove(site)
                summary = site.finalize()
                self.completed_sites.append(summary)
        return items

    def check_finalize_sites(self):
        """检查活跃站点中是否有完成的"""
        sites_done = []
        for site in self.active_sites:
            if site.is_done():
                sites_done.append(site)
        for site in sites_done:
            self.active_sites.remove(site)
            summary = site.finalize()
            self.completed_sites.append(summary)

    def refill_active(self, target=256):
        while len(self.active_sites) < target and self.sites:
            self.active_sites.append(self.sites.pop(0))

    def write_stats(self):
        elapsed = time.time() - self.started
        rate = self.total_fetched / max(0.001, elapsed)
        stat = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(elapsed, 1),
            "total_fetched": self.total_fetched,
            "total_unique_text": self.total_unique,
            "pages_per_second": round(rate, 2),
            "active_sites": len(self.active_sites),
            "remaining_sites": len(self.sites),
            "completed_sites": len(self.completed_sites),
            "in_flight": getattr(self, "_in_flight", 0),
        }
        with self.stats_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(stat, ensure_ascii=False) + "\n")
        self.latest_path.write_text(json.dumps(stat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[{stat['timestamp']}] fetched={self.total_fetched} unique={self.total_unique} rate={rate:.1f}/s active={len(self.active_sites)} queue={len(self.sites)} done={len(self.completed_sites)} inflight={stat['in_flight']}", flush=True)

    async def _run_loop(self, session):
        self.refill_active(target=self.args.active_sites)
        last_stats = time.time()
        pending = set()
        self._in_flight = 0

        while self.active_sites or self.sites or pending:
            self.refill_active(target=self.args.active_sites)

            # 持续向 pending 集合填充任务直到达到并发上限
            fill_count = self.max_concurrent - len(pending)
            if fill_count > 0 and self.active_sites:
                items = self.collect_items(fill_count)
                for site, item in items:
                    task = asyncio.ensure_future(self.fetch_one(session, site, item))
                    pending.add(task)
                self._in_flight = len(pending)

            if not pending:
                break

            # 等待至少一个完成
            done, pending = await asyncio.wait(pending, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except Exception:
                    pass
            self._in_flight = len(pending)
            self.check_finalize_sites()

            now = time.time()
            if now - last_stats >= 10:
                self.write_stats()
                last_stats = now

    async def run(self):
        if self.use_cffi:
            print(f"HTTP backend: curl_cffi (TLS/JA3 浏览器指纹伪装, max_clients={self.max_concurrent})", flush=True)
            async with CffiAsyncSession(max_clients=self.max_concurrent) as session:
                await self._run_loop(session)
        else:
            reason = "curl_cffi 未安装" if CffiAsyncSession is None else "--no-impersonate"
            print(f"HTTP backend: aiohttp (无 TLS 指纹伪装, {reason})", flush=True)
            connector = aiohttp.TCPConnector(
                limit=self.max_concurrent,
                limit_per_host=self.max_per_host,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
                force_close=False,
            )
            headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}
            async with aiohttp.ClientSession(connector=connector, timeout=self.timeout, headers=headers) as session:
                await self._run_loop(session)

        self.write_stats()
        # 合并 partial JSONL
        merged = 0
        with self.partial_path.open("a", encoding="utf-8") as out:
            for summary in self.completed_sites:
                scope = summary.get("scope_domain", "")
                pages_path = Path(self.args.output_root) / scope / "pages.jsonl"
                if pages_path.exists() and pages_path.stat().st_size > 0:
                    try:
                        out.write(pages_path.read_text(encoding="utf-8"))
                        merged += 1
                    except Exception:
                        pass
        print(f"\nDone. Total fetched: {self.total_fetched}, unique: {self.total_unique}, completed sites: {len(self.completed_sites)}, merged to partial: {merged}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Async high-throughput multi-site crawler")
    parser.add_argument("--seeds-tsv", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--max-sites", type=int, default=20000)
    parser.add_argument("--pages-per-site", type=int, default=100000)
    parser.add_argument("--max-discovered", type=int, default=2000000)
    parser.add_argument("--max-depth", type=int, default=50)
    parser.add_argument("--max-concurrent", type=int, default=2048, help="全局最大并发连接")
    parser.add_argument("--max-per-host", type=int, default=8, help="每 host 最大并发")
    parser.add_argument("--active-sites", type=int, default=512, help="同时活跃站点数")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--max-page-bytes", type=int, default=2000000)
    parser.add_argument("--stats-jsonl", default="cc_async_run_stats.jsonl")
    parser.add_argument("--partial-jsonl", default="cc_async_pages_partial.jsonl")
    parser.add_argument("--latest-json", default="cc_async_run_latest.json")
    parser.add_argument(
        "--impersonate", action=argparse.BooleanOptionalAction, default=True,
        help="用 curl_cffi 模拟真实浏览器 TLS/JA3/HTTP2 指纹 (需要 pip install curl_cffi); "
             "--no-impersonate 强制退回 aiohttp 裸连接",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    engine = AsyncCrawlEngine(args)
    engine.load_seeds(
        args.seeds_tsv,
        args.max_sites,
        args.pages_per_site,
        args.max_discovered,
        args.max_depth,
        args.output_root,
    )
    asyncio.run(engine.run())


if __name__ == "__main__":
    main()
