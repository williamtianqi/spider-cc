#!/usr/bin/env python3
import argparse
import csv
import gzip
import hashlib
import json
import re
import threading
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urldefrag, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

import browser_fingerprint as fp

try:
    import trafilatura
    from trafilatura.metadata import extract_metadata
except Exception:
    trafilatura = None
    extract_metadata = None

try:
    import curl_cffi.requests as cffi_requests
    from curl_cffi.requests import RequestsError as CffiRequestsError
except ImportError:
    cffi_requests = None
    CffiRequestsError = Exception

USER_AGENT = "domain-link-crawler/0.1"
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
COMMON_SITEMAP_PATHS = [
    "/sitemap.xml", "/sitemap.xml.gz", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap1.xml", "/sitemaps.xml", "/wp-sitemap.xml", "/post-sitemap.xml",
    "/page-sitemap.xml", "/news-sitemap.xml", "/sitemap-news.xml", "/sitemap_news.xml",
    "/sitemap/sitemap.xml", "/sitemaps/sitemap.xml",
]
COMMON_FEED_PATHS = ["/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml"]


class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.feed_links = []
        self.sitemap_links = []
        self.canonical = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_dict = {k.lower(): v for k, v in attrs if k and v}
        href = attrs_dict.get("href")
        if tag == "a" and href:
            self.links.append(href)
        if tag == "link" and href:
            rel = attrs_dict.get("rel", "").lower()
            link_type = attrs_dict.get("type", "").lower()
            if "canonical" in rel:
                self.canonical = href
            if "alternate" in rel and ("rss" in link_type or "atom" in link_type):
                self.feed_links.append(href)
            if "sitemap" in rel or "sitemap" in href.lower():
                self.sitemap_links.append(href)


class FallbackTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.title_parts = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas", "template", "nav", "footer", "header"}:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in {"p", "article", "section", "main", "h1", "h2", "h3", "li", "tr", "br"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "canvas", "template", "nav", "footer", "header"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in {"p", "article", "section", "main", "h1", "h2", "h3", "li", "tr"}:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        data = data.strip()
        if not data:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)

    def result(self):
        return clean_text(" ".join(self.title_parts)), clean_text("\n".join(self.text_parts))


def clean_text(value):
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def header_get(headers, name, default=""):
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return default


def fetch_bytes_once(url, timeout, max_bytes, extra_headers=None):
    """抓取 url 原始字节。优先用 curl_cffi 做浏览器 TLS/JA3 指纹伪装
    (impersonate=按域名稳定选择的 chrome/firefox/safari profile), curl_cffi
    未安装时回退到 urllib + 按域名轮换的真实浏览器 UA 字符串。

    返回 (status, final_url, headers_dict, data_bytes)；非 2xx 或 304 会
    抛出 urllib.error.HTTPError(与两条后端路径保持一致), 由调用方 fetch_bytes
    / fetch_worker 按 exc.code 做重试或分类。
    """
    if cffi_requests is not None:
        return _fetch_bytes_once_cffi(url, timeout, max_bytes, extra_headers)
    return _fetch_bytes_once_urllib(url, timeout, max_bytes, extra_headers)


def _fetch_bytes_once_urllib(url, timeout, max_bytes, extra_headers=None):
    host = host_of(url)
    headers = {
        "User-Agent": fp.user_agent_for_host(host),
        "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,*/*;q=0.8",
        "Accept-Encoding": "gzip",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as response:
        chunks = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"response_too_large:{total}>{max_bytes}")
            chunks.append(chunk)
        data = b"".join(chunks)
        headers = dict(response.headers.items())
        status = getattr(response, "status", None)
        final_url = response.geturl()
        if header_get(headers, "Content-Encoding").lower() == "gzip":
            data = gzip.decompress(data)
        return status, final_url, headers, data


def _fetch_bytes_once_cffi(url, timeout, max_bytes, extra_headers=None):
    host = host_of(url)
    profile = fp.profile_for_host(host)
    headers = {}
    if extra_headers:
        headers.update(extra_headers)
    response = None
    try:
        response = cffi_requests.get(
            url,
            headers=headers or None,
            impersonate=profile,
            timeout=timeout,
            allow_redirects=True,
            max_redirects=5,
            stream=True,
        )
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"response_too_large:{total}>{max_bytes}")
            chunks.append(chunk)
        data = b"".join(chunks)
        headers_dict = dict(response.headers.items())
        status = response.status_code
        final_url = response.url
    except CffiRequestsError as exc:
        raise OSError(f"curl_cffi_error:{exc}") from exc
    finally:
        if response is not None:
            response.close()
    if status == 304 or status >= 400:
        msg = Message()
        for key, value in headers_dict.items():
            msg[key] = value
        raise HTTPError(url, status, str(status), msg, None)
    return status, final_url, headers_dict, data


def fetch_bytes(url, timeout, max_bytes, retries=2, backoff_seconds=1.0, extra_headers=None):
    last_error = None
    for attempt in range(retries + 1):
        try:
            return fetch_bytes_once(url, timeout, max_bytes, extra_headers=extra_headers)
        except ValueError:
            raise
        except HTTPError as exc:
            last_error = exc
            if exc.code == 304 or (400 <= exc.code < 500 and exc.code != 429):
                raise
            if attempt < retries:
                time.sleep(retry_after_seconds(exc, backoff_seconds * (attempt + 1)))
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff_seconds * (attempt + 1))
    raise last_error


def retry_after_seconds(http_error, default_seconds):
    header_value = None
    try:
        header_value = http_error.headers.get("Retry-After") if http_error.headers else None
    except Exception:
        header_value = None
    if not header_value:
        return default_seconds
    try:
        return min(float(header_value), 30.0)
    except ValueError:
        return default_seconds


def normalize_url(raw_url, base_url=None, allow_static=False):
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
    if not allow_static and any(path.lower().endswith(ext) for ext in STATIC_EXTENSIONS):
        return None
    path = quote(path, safe="/%:@")
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        key_lower = key.lower()
        if key_lower in DROP_QUERY_KEYS or any(key_lower.startswith(prefix) for prefix in DROP_QUERY_PREFIXES):
            continue
        query_pairs.append((key, value))
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)
    return urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))


def url_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def content_hash(text):
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def root_url(url):
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def host_of(url):
    return (urlparse(url).hostname or "").lower()


def in_scope(url, allowed_hosts, allowed_domains, include_subdomains):
    host = host_of(url)
    if allowed_hosts and host in allowed_hosts:
        return True
    for domain in allowed_domains:
        if host == domain:
            return True
        if include_subdomains and host.endswith("." + domain):
            return True
    return False


def robots_for_url(url, robots_cache, timeout, max_bytes):
    root = root_url(url)
    if root in robots_cache:
        return robots_cache[root]
    robots_url = urljoin(root, "/robots.txt")
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    sitemap_urls = []
    status = None
    try:
        status, final_url, headers, data = fetch_bytes(robots_url, timeout, max_bytes)
        text = data.decode("utf-8", errors="replace")
        rp.parse(text.splitlines())
        for line in text.splitlines():
            if line.lower().startswith("sitemap:"):
                normalized = normalize_url(line.split(":", 1)[1].strip(), robots_url, allow_static=True)
                if normalized:
                    sitemap_urls.append(normalized)
    except Exception:
        rp.parse([])
    item = {"parser": rp, "robots_url": robots_url, "status": status, "sitemaps": list(dict.fromkeys(sitemap_urls))}
    robots_cache[root] = item
    return item


def can_fetch(url, robots_cache, timeout, max_bytes):
    return robots_for_url(url, robots_cache, timeout, max_bytes)["parser"].can_fetch(USER_AGENT, url)


def looks_like_xml(data, headers):
    content_type = header_get(headers, "Content-Type").lower()
    prefix = data[:500].lstrip().lower()
    return "xml" in content_type or prefix.startswith(b"<?xml") or b"<urlset" in prefix or b"<sitemapindex" in prefix or b"<rss" in prefix or b"<feed" in prefix


def parse_sitemap_xml(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return [], []
    namespace = ""
    if root.tag.startswith("{"):
        namespace = root.tag.split("}", 1)[0] + "}"
    sitemap_urls = []
    page_urls = []
    if root.tag.endswith("sitemapindex"):
        for item in root.findall(f"{namespace}sitemap"):
            loc = item.findtext(f"{namespace}loc")
            lastmod = item.findtext(f"{namespace}lastmod")
            if loc:
                sitemap_urls.append({"url": loc.strip(), "lastmod": lastmod or ""})
    if root.tag.endswith("urlset"):
        for item in root.findall(f"{namespace}url"):
            loc = item.findtext(f"{namespace}loc")
            lastmod = item.findtext(f"{namespace}lastmod")
            if loc:
                page_urls.append({"url": loc.strip(), "lastmod": lastmod or ""})
    return sitemap_urls, page_urls


def parse_feed_xml(xml_bytes, base_url):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    urls = []
    for elem in root.iter():
        tag = elem.tag.lower()
        if tag.endswith("link"):
            href = elem.attrib.get("href")
            rel = elem.attrib.get("rel", "alternate").lower()
            text = elem.text.strip() if elem.text else ""
            candidate = href or text
            if candidate and rel in {"alternate", ""}:
                normalized = normalize_url(candidate, base_url)
                if normalized:
                    urls.append(normalized)
    return urls


def common_sitemap_urls(seed_url):
    root = root_url(seed_url).rstrip("/")
    return [root + path for path in COMMON_SITEMAP_PATHS]


def common_feed_urls(seed_url):
    root = root_url(seed_url).rstrip("/")
    return [root + path for path in COMMON_FEED_PATHS]


def extract_page_links(html, base_url):
    parser = LinkExtractor()
    parser.feed(html)
    links = [normalize_url(link, base_url) for link in parser.links]
    feeds = [normalize_url(link, base_url) for link in parser.feed_links]
    sitemaps = [normalize_url(link, base_url) for link in parser.sitemap_links]
    canonical = normalize_url(parser.canonical, base_url) if parser.canonical else ""
    return {
        "links": [item for item in links if item],
        "feeds": [item for item in feeds if item],
        "sitemaps": [item for item in sitemaps if item],
        "canonical": canonical,
    }


def extract_content(html, url):
    title = ""
    text = ""
    extractor = "fallback"
    if trafilatura is not None:
        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_precision=True,
            output_format="txt",
        )
        if extracted:
            text = clean_text(extracted)
            extractor = "trafilatura"
        if extract_metadata is not None:
            metadata = extract_metadata(html)
            if metadata and metadata.title:
                title = metadata.title.strip()
    if not text or not title:
        fallback = FallbackTextExtractor()
        fallback.feed(html)
        fallback_title, fallback_text = fallback.result()
        if not title:
            title = fallback_title
        if not text:
            text = fallback_text
    return title, text, extractor


def enqueue_url(queue, seen, discovered_rows, url, source, depth, from_url, allowed_hosts, allowed_domains, include_subdomains, robots_cache, timeout, max_bytes):
    normalized = normalize_url(url, from_url)
    if not normalized:
        return "normalize_failed"
    digest = url_hash(normalized)
    if digest in seen:
        return "duplicate_url"
    if not in_scope(normalized, allowed_hosts, allowed_domains, include_subdomains):
        return "out_of_scope"
    if not can_fetch(normalized, robots_cache, timeout, max_bytes):
        return "robots_disallow"
    seen.add(digest)
    queue.append({"url": normalized, "source": source, "depth": depth, "from_url": from_url or "", "url_hash": digest})
    discovered_rows.append({"url": normalized, "source": source, "depth": depth, "from_url": from_url or "", "url_hash": digest})
    return "queued"


def discover_sitemaps(seed_url, allowed_hosts, allowed_domains, include_subdomains, robots_cache, timeout, max_page_bytes, max_sitemap_bytes, max_sitemaps):
    robots_item = robots_for_url(seed_url, robots_cache, timeout, max_page_bytes)
    primary_seeds = list(dict.fromkeys(robots_item["sitemaps"]))
    fallback_seeds = common_sitemap_urls(seed_url)
    seen_sitemaps = set()
    page_urls = []
    reports = []

    def crawl_sitemap_seeds(seeds):
        sitemap_queue = deque(dict.fromkeys(seeds))
        local_pages = []
        while sitemap_queue and len(seen_sitemaps) < max_sitemaps:
            sitemap_url = sitemap_queue.popleft()
            normalized_sitemap = normalize_url(sitemap_url, allow_static=True)
            if not normalized_sitemap or normalized_sitemap in seen_sitemaps:
                continue
            if not in_scope(normalized_sitemap, allowed_hosts, allowed_domains, include_subdomains):
                continue
            seen_sitemaps.add(normalized_sitemap)
            try:
                status, final_url, headers, data = fetch_bytes(normalized_sitemap, timeout, max_sitemap_bytes)
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                if status != 200 or not looks_like_xml(data, headers):
                    reports.append({"url": normalized_sitemap, "ok": False, "status": status, "error": "not_xml"})
                    continue
                child_sitemaps, pages = parse_sitemap_xml(data)
                for child in child_sitemaps:
                    child_url = normalize_url(child["url"], final_url, allow_static=True)
                    if child_url and child_url not in seen_sitemaps:
                        sitemap_queue.append(child_url)
                for page in pages:
                    normalized = normalize_url(page["url"], final_url)
                    if normalized and in_scope(normalized, allowed_hosts, allowed_domains, include_subdomains):
                        local_pages.append({"url": normalized, "lastmod": page.get("lastmod", "")})
                reports.append({"url": normalized_sitemap, "ok": True, "status": status, "pages": len(pages), "child_sitemaps": len(child_sitemaps), "error": ""})
            except Exception as exc:
                reports.append({"url": normalized_sitemap, "ok": False, "status": "", "error": repr(exc)})
        return local_pages

    page_urls.extend(crawl_sitemap_seeds(primary_seeds))
    if not page_urls:
        page_urls.extend(crawl_sitemap_seeds(fallback_seeds))
    return page_urls, reports


def fetch_feed_urls(feed_url, timeout, max_bytes):
    status, final_url, headers, data = fetch_bytes(feed_url, timeout, max_bytes)
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if status != 200 or not looks_like_xml(data, headers):
        return []
    return parse_feed_xml(data, final_url)


def fetch_sitemap_page_urls(sitemap_url, allowed_hosts, allowed_domains, include_subdomains, timeout, max_sitemap_bytes):
    normalized_sitemap = normalize_url(sitemap_url, allow_static=True)
    if not normalized_sitemap:
        return []
    status, final_url, headers, data = fetch_bytes(normalized_sitemap, timeout, max_sitemap_bytes)
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if status != 200 or not looks_like_xml(data, headers):
        return []
    child_sitemaps, pages = parse_sitemap_xml(data)
    urls = []
    for page in pages:
        normalized = normalize_url(page["url"], final_url)
        if normalized and in_scope(normalized, allowed_hosts, allowed_domains, include_subdomains):
            urls.append(normalized)
    for child in child_sitemaps[:50]:
        child_url = normalize_url(child["url"], final_url, allow_static=True)
        if child_url and in_scope(child_url, allowed_hosts, allowed_domains, include_subdomains):
            try:
                urls.extend(fetch_sitemap_page_urls(child_url, allowed_hosts, allowed_domains, include_subdomains, timeout, max_sitemap_bytes))
            except Exception:
                continue
    return urls


def crawl_one(item, timeout, max_page_bytes, save_html, html_dir, write_full_text, host_semaphores, host_semaphores_lock, max_host_workers):
    url = item["url"]
    record = {"url": url, "url_hash": item["url_hash"], "source": item["source"], "depth": item["depth"], "from_url": item["from_url"], "ok": False}
    semaphore = get_host_semaphore(url, host_semaphores, host_semaphores_lock, max_host_workers)
    if semaphore:
        semaphore.acquire()
    try:
        status, final_url, headers, data = fetch_bytes(url, timeout, max_page_bytes)
        content_type = header_get(headers, "Content-Type")
        record.update({"status": status, "final_url": final_url, "content_type": content_type, "bytes": len(data)})
        if status != 200 or "html" not in content_type.lower():
            record["error"] = "non_html_or_non_200"
            return record, None, []
        html = data.decode("utf-8", errors="replace")
        page_links = extract_page_links(html, final_url)
        title, text, extractor = extract_content(html, final_url)
        c_hash = content_hash(text)
        record.update({"ok": True, "title": title, "text_length": len(text), "content_hash": c_hash, "extractor": extractor, "canonical": page_links["canonical"]})
        if save_html:
            html_dir.mkdir(parents=True, exist_ok=True)
            html_name = hashlib.sha1(final_url.encode("utf-8")).hexdigest() + ".html"
            html_path = html_dir / html_name
            html_path.write_bytes(data)
            record["html_file"] = str(html_path)
        extracted = {
            "url": final_url,
            "source": item["source"],
            "depth": item["depth"],
            "title": title,
            "text_length": len(text),
            "content_hash": c_hash,
            "extractor": extractor,
            "text_preview": text[:1000],
        }
        if write_full_text:
            extracted["text"] = text
        outlinks = [{"url": link, "source": "page_link", "from_url": final_url, "depth": item["depth"] + 1} for link in page_links["links"]]
        outlinks.extend({"url": feed, "source": "feed", "from_url": final_url, "depth": item["depth"] + 1} for feed in page_links["feeds"])
        outlinks.extend({"url": sitemap, "source": "sitemap_link", "from_url": final_url, "depth": item["depth"] + 1} for sitemap in page_links["sitemaps"])
        return record, extracted, outlinks
    except Exception as exc:
        record["error"] = repr(exc)
        return record, None, []
    finally:
        if semaphore:
            semaphore.release()


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def save_frontier(output_dir, queue):
    rows = list(queue)
    write_jsonl(output_dir / "frontier_remaining.jsonl", rows)


def log_line(log_path, message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def progress_snapshot(started, args, crawled, queue, discovered_rows, manifest_rows, extracted_rows, link_rows, reject_counts):
    elapsed = max(0.001, time.time() - started)
    ok_count = sum(1 for row in manifest_rows if row.get("ok"))
    duplicate_content_count = sum(1 for row in manifest_rows if row.get("content_duplicate"))
    return {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(elapsed, 2),
        "pages_per_second": round(crawled / elapsed, 4),
        "crawled_pages": crawled,
        "target_max_pages": args.max_pages,
        "discovered_urls": len(discovered_rows),
        "remaining_frontier": len(queue),
        "manifest_records": len(manifest_rows),
        "ok_manifest_records": ok_count,
        "unique_extracted_text_records": len(extracted_rows),
        "duplicate_content_records": duplicate_content_count,
        "link_edges": len(link_rows),
        "reject_counts": dict(reject_counts),
        "limits": {
            "max_workers": args.max_workers,
            "max_host_workers": args.max_host_workers,
            "batch_size": args.batch_size,
            "max_depth": args.max_depth,
            "max_discovered": args.max_discovered,
        },
    }


def write_progress(output_dir, snapshot):
    (output_dir / "progress.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_host_semaphore(url, host_semaphores, host_semaphores_lock, max_host_workers):
    if max_host_workers <= 0:
        return None
    host = host_of(url)
    with host_semaphores_lock:
        if host not in host_semaphores:
            host_semaphores[host] = threading.BoundedSemaphore(max_host_workers)
        return host_semaphores[host]


def parse_args():
    parser = argparse.ArgumentParser(description="Domain-wide internal link crawler: discover all in-scope links and extract text")
    parser.add_argument("--seed-url", required=True)
    parser.add_argument("--allowed-host", action="append", dest="allowed_hosts")
    parser.add_argument("--allowed-domain", action="append", dest="allowed_domains")
    parser.add_argument("--include-subdomains", action="store_true")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--max-discovered", type=int, default=100000)
    parser.add_argument("--max-depth", type=int, default=10)
    parser.add_argument("--max-sitemaps", type=int, default=200)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--max-host-workers", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--progress-interval", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--max-page-bytes", type=int, default=2000000)
    parser.add_argument("--max-sitemap-bytes", type=int, default=50000000)
    parser.add_argument("--save-html", action="store_true")
    parser.add_argument("--write-full-text", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    seed_url = normalize_url(args.seed_url)
    if not seed_url:
        raise ValueError("Invalid seed URL")
    seed_host = host_of(seed_url)
    allowed_hosts = set(args.allowed_hosts or [seed_host])
    allowed_domains = set(args.allowed_domains or [])
    if not allowed_domains and not allowed_hosts:
        allowed_hosts.add(seed_host)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    html_dir = output_dir / "html"
    log_path = output_dir / "crawl.log"
    log_path.write_text("", encoding="utf-8")
    started = time.time()
    robots_cache = {}
    queue = deque()
    seen = set()
    discovered_rows = []
    manifest_rows = []
    extracted_rows = []
    link_rows = []
    reject_counts = Counter()
    duplicate_content_hashes = set()
    host_semaphores = {}
    host_semaphores_lock = threading.Lock()
    next_progress_at = max(1, args.progress_interval)

    log_line(log_path, f"start seed={seed_url} max_pages={args.max_pages} max_workers={args.max_workers} max_host_workers={args.max_host_workers} batch_size={args.batch_size}")
    sitemap_pages, sitemap_reports = discover_sitemaps(seed_url, allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes, args.max_sitemap_bytes, args.max_sitemaps)
    log_line(log_path, f"sitemap_discovery pages={len(sitemap_pages)} reports={len(sitemap_reports)}")
    reject_counts[enqueue_url(queue, seen, discovered_rows, seed_url, "seed", 0, "", allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes)] += 1
    for page in sitemap_pages:
        if len(discovered_rows) >= args.max_discovered:
            break
        reject_counts[enqueue_url(queue, seen, discovered_rows, page["url"], "sitemap", 0, "", allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes)] += 1

    for feed in common_feed_urls(seed_url):
        try:
            if can_fetch(feed, robots_cache, args.timeout, args.max_page_bytes):
                for feed_url in fetch_feed_urls(feed, args.timeout, args.max_page_bytes):
                    if len(discovered_rows) >= args.max_discovered:
                        break
                    reject_counts[enqueue_url(queue, seen, discovered_rows, feed_url, "feed", 0, feed, allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes)] += 1
        except Exception:
            continue

    crawled = 0
    while queue and crawled < args.max_pages and len(discovered_rows) <= args.max_discovered:
        batch = []
        while queue and len(batch) < args.batch_size and crawled + len(batch) < args.max_pages:
            item = queue.popleft()
            if item["depth"] <= args.max_depth:
                batch.append(item)
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures = [
                executor.submit(
                    crawl_one,
                    item,
                    args.timeout,
                    args.max_page_bytes,
                    args.save_html,
                    html_dir,
                    args.write_full_text,
                    host_semaphores,
                    host_semaphores_lock,
                    args.max_host_workers,
                )
                for item in batch
            ]
            for future in as_completed(futures):
                record, extracted, outlinks = future.result()
                manifest_rows.append(record)
                crawled += 1
                if extracted:
                    if extracted["content_hash"] not in duplicate_content_hashes:
                        duplicate_content_hashes.add(extracted["content_hash"])
                        extracted_rows.append(extracted)
                    else:
                        record["content_duplicate"] = True
                for outlink in outlinks:
                    link_rows.append({"from_url": outlink["from_url"], "to_url": outlink["url"], "source": outlink["source"]})
                    if outlink["depth"] > args.max_depth or len(discovered_rows) >= args.max_discovered:
                        continue
                    if outlink["source"] == "feed":
                        try:
                            for feed_url in fetch_feed_urls(outlink["url"], args.timeout, args.max_page_bytes):
                                if len(discovered_rows) >= args.max_discovered:
                                    break
                                result = enqueue_url(queue, seen, discovered_rows, feed_url, "feed_item", outlink["depth"], outlink["url"], allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes)
                                reject_counts[result] += 1
                        except Exception:
                            reject_counts["feed_parse_failed"] += 1
                        continue
                    if outlink["source"] == "sitemap_link":
                        try:
                            for sitemap_page_url in fetch_sitemap_page_urls(outlink["url"], allowed_hosts, allowed_domains, args.include_subdomains, args.timeout, args.max_sitemap_bytes):
                                if len(discovered_rows) >= args.max_discovered:
                                    break
                                result = enqueue_url(queue, seen, discovered_rows, sitemap_page_url, "sitemap_link_page", outlink["depth"], outlink["url"], allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes)
                                reject_counts[result] += 1
                        except Exception:
                            reject_counts["sitemap_link_parse_failed"] += 1
                        continue
                    result = enqueue_url(queue, seen, discovered_rows, outlink["url"], outlink["source"], outlink["depth"], outlink["from_url"], allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes)
                    reject_counts[result] += 1
                if crawled >= next_progress_at:
                    snapshot = progress_snapshot(started, args, crawled, queue, discovered_rows, manifest_rows, extracted_rows, link_rows, reject_counts)
                    write_progress(output_dir, snapshot)
                    log_line(log_path, f"progress crawled={snapshot['crawled_pages']} rate={snapshot['pages_per_second']}/s discovered={snapshot['discovered_urls']} frontier={snapshot['remaining_frontier']} ok={snapshot['ok_manifest_records']} unique_text={snapshot['unique_extracted_text_records']}")
                    next_progress_at += max(1, args.progress_interval)

    final_snapshot = progress_snapshot(started, args, crawled, queue, discovered_rows, manifest_rows, extracted_rows, link_rows, reject_counts)
    write_progress(output_dir, final_snapshot)
    log_line(log_path, f"finished crawled={final_snapshot['crawled_pages']} rate={final_snapshot['pages_per_second']}/s discovered={final_snapshot['discovered_urls']} frontier={final_snapshot['remaining_frontier']} ok={final_snapshot['ok_manifest_records']} unique_text={final_snapshot['unique_extracted_text_records']}")

    write_jsonl(output_dir / "manifest.jsonl", manifest_rows)
    write_jsonl(output_dir / "extracted_text.jsonl", extracted_rows)
    write_csv(output_dir / "discovered_urls.csv", discovered_rows, ["url", "source", "depth", "from_url", "url_hash"])
    write_csv(output_dir / "links.csv", link_rows, ["from_url", "to_url", "source"])
    write_jsonl(output_dir / "sitemap_reports.jsonl", sitemap_reports)
    save_frontier(output_dir, queue)
    summary = {
        "seed_url": seed_url,
        "allowed_hosts": sorted(allowed_hosts),
        "allowed_domains": sorted(allowed_domains),
        "include_subdomains": args.include_subdomains,
        "extractor": "trafilatura" if trafilatura is not None else "fallback",
        "sitemap_pages": len(sitemap_pages),
        "sitemap_reports": len(sitemap_reports),
        "discovered_urls": len(discovered_rows),
        "crawled_pages": len(manifest_rows),
        "successful_text_pages": len(extracted_rows),
        "remaining_frontier": len(queue),
        "reject_counts": dict(reject_counts),
        "elapsed_seconds": round(time.time() - started, 2),
        "limits": {
            "max_pages": args.max_pages,
            "max_discovered": args.max_discovered,
            "max_depth": args.max_depth,
            "max_sitemaps": args.max_sitemaps,
            "max_workers": args.max_workers,
            "max_host_workers": args.max_host_workers,
            "batch_size": args.batch_size,
            "progress_interval": args.progress_interval,
        },
        "files": {
            "discovered_urls": str(output_dir / "discovered_urls.csv"),
            "links": str(output_dir / "links.csv"),
            "manifest": str(output_dir / "manifest.jsonl"),
            "extracted_text": str(output_dir / "extracted_text.jsonl"),
            "sitemap_reports": str(output_dir / "sitemap_reports.jsonl"),
            "frontier_remaining": str(output_dir / "frontier_remaining.jsonl"),
            "progress": str(output_dir / "progress.json"),
            "log": str(output_dir / "crawl.log"),
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
