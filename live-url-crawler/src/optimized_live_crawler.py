#!/usr/bin/env python3
import argparse
import csv
import gzip
import hashlib
import json
import math
import re
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urldefrag, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

try:
    import trafilatura
    from trafilatura.metadata import extract_metadata
except Exception:
    trafilatura = None
    extract_metadata = None

USER_AGENT = "optimized-live-crawler-demo/0.2"
LOW_VALUE_PATH_PARTS = {
    "login",
    "signup",
    "register",
    "cart",
    "checkout",
    "search",
    "tag",
    "tags",
    "calendar",
    "account",
    "author",
    "category",
    "archive",
    "privacy",
    "terms",
}
STATIC_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".rar",
    ".7z",
    ".pdf",
    ".epub",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".woff",
    ".woff2",
    ".ttf",
}
DROP_QUERY_PREFIXES = ("utm_", "gad_")
DROP_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "sessionid",
    "sid",
    "spm",
    "ref",
    "source",
    "from",
    "__hsfp",
    "__hssc",
    "__hstc",
    "gbraid",
    "wbraid",
    "msclkid",
    "trk",
    "trkcampaign",
}
COMMON_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap.xml.gz",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap1.xml",
    "/sitemaps.xml",
    "/wp-sitemap.xml",
    "/post-sitemap.xml",
    "/page-sitemap.xml",
    "/news-sitemap.xml",
    "/sitemap-news.xml",
    "/sitemap_news.xml",
    "/sitemap/sitemap.xml",
    "/sitemaps/sitemap.xml",
]
COMMON_FEED_PATHS = [
    "/feed",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/feed.xml",
    "/index.xml",
]


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


class SimhashDeduper:
    def __init__(self, hamming_threshold):
        self.hamming_threshold = hamming_threshold
        self.buckets = defaultdict(list)
        self.exact_hashes = set()

    def check_and_add(self, text):
        normalized = normalize_text_for_hash(text)
        exact_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if exact_hash in self.exact_hashes:
            return False, "exact_content_duplicate", exact_hash, ""
        simhash = compute_simhash(normalized)
        simhash_hex = f"{simhash:016x}"
        for bucket in simhash_buckets(simhash):
            for old_hash in self.buckets[bucket]:
                if hamming_distance(simhash, old_hash) <= self.hamming_threshold:
                    return False, "near_semantic_duplicate", exact_hash, simhash_hex
        self.exact_hashes.add(exact_hash)
        for bucket in simhash_buckets(simhash):
            self.buckets[bucket].append(simhash)
        return True, "unique", exact_hash, simhash_hex


def clean_text(value):
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def normalize_text_for_hash(text):
    text = text.lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def tokenize(text):
    return [token for token in re.findall(r"\w+", text.lower(), flags=re.UNICODE) if len(token) >= 3]


def compute_simhash(text):
    tokens = tokenize(text)
    if len(tokens) >= 5:
        features = [" ".join(tokens[i:i + 3]) for i in range(max(1, len(tokens) - 2))]
    else:
        features = tokens
    weights = Counter(features)
    vector = [0] * 64
    for feature, weight in weights.items():
        digest = int(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).hexdigest(), 16)
        for bit in range(64):
            if digest & (1 << bit):
                vector[bit] += weight
            else:
                vector[bit] -= weight
    value = 0
    for bit, score in enumerate(vector):
        if score >= 0:
            value |= 1 << bit
    return value


def simhash_buckets(value):
    return [f"{band}:{(value >> (band * 16)) & 0xffff:04x}" for band in range(4)]


def hamming_distance(left, right):
    return (left ^ right).bit_count()


def header_get(headers, name, default=""):
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return default


def fetch_bytes(url, timeout, max_bytes):
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,*/*;q=0.8",
            "Accept-Encoding": "gzip",
        },
    )
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


def same_site(url, allowed_hosts):
    host = urlparse(url).hostname or ""
    return host.lower() in allowed_hosts


def is_low_value_url(url):
    parsed = urlparse(url)
    parts = {p for p in re.split(r"[^a-z0-9]+", parsed.path.lower()) if p}
    if parts & LOW_VALUE_PATH_PARTS:
        return True
    if len(parse_qsl(parsed.query, keep_blank_values=True)) > 3:
        return True
    if re.search(r"/(page|p)/\d{4,}(/|$)", parsed.path.lower()):
        return True
    return False


def path_allowed(url, include_prefixes):
    if not include_prefixes:
        return True
    path = urlparse(url).path or "/"
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in include_prefixes)


def safe_name(url, index):
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", parsed.path.strip("/")).strip("-")[:80]
    if not slug:
        slug = "home"
    return f"{index:06d}_{host}_{slug}_{digest}.html"


def root_url(base_url):
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def robots_parser(base_url, timeout, max_bytes):
    robots_url = urljoin(root_url(base_url), "/robots.txt")
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
                value = line.split(":", 1)[1].strip()
                normalized = normalize_url(value, robots_url)
                if normalized:
                    sitemap_urls.append(normalized)
    except Exception:
        rp.parse([])
    return rp, robots_url, status, list(dict.fromkeys(sitemap_urls))


def discover_homepage_sources(base_url, timeout, max_bytes):
    status, final_url, headers, data = fetch_bytes(base_url, timeout, max_bytes)
    html = data.decode("utf-8", errors="replace")
    parser = LinkExtractor()
    parser.feed(html)
    links = [normalize_url(link, final_url) for link in parser.links]
    feeds = [normalize_url(link, final_url) for link in parser.feed_links]
    sitemaps = [normalize_url(link, final_url) for link in parser.sitemap_links]
    return {
        "status": status,
        "final_url": final_url,
        "links": [item for item in links if item],
        "feeds": [item for item in feeds if item],
        "sitemaps": [item for item in sitemaps if item],
    }


def common_sitemap_urls(base_url):
    root = root_url(base_url).rstrip("/")
    return [root + path for path in COMMON_SITEMAP_PATHS]


def common_feed_urls(base_url):
    root = root_url(base_url).rstrip("/")
    return [root + path for path in COMMON_FEED_PATHS]


def looks_like_sitemap(data, headers):
    content_type = header_get(headers, "Content-Type").lower()
    prefix = data[:500].lstrip().lower()
    return "xml" in content_type or prefix.startswith(b"<?xml") or b"<urlset" in prefix or b"<sitemapindex" in prefix


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
            changefreq = item.findtext(f"{namespace}changefreq")
            priority = item.findtext(f"{namespace}priority")
            if loc:
                page_urls.append({"url": loc.strip(), "lastmod": lastmod or "", "changefreq": changefreq or "", "priority": priority or ""})
    return sitemap_urls, page_urls


def fetch_one_sitemap(sitemap_url, timeout, max_bytes):
    status, final_url, headers, data = fetch_bytes(sitemap_url, timeout, max_bytes)
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if status != 200 or not looks_like_sitemap(data, headers):
        return {"sitemap_url": sitemap_url, "status": status, "ok": False, "child_sitemaps": [], "pages": [], "error": "not_sitemap"}
    child_sitemaps, pages = parse_sitemap_xml(data)
    return {"sitemap_url": sitemap_url, "status": status, "ok": True, "child_sitemaps": child_sitemaps, "pages": pages, "error": ""}


def discover_sitemap_pages(seed_sitemaps, max_sitemaps, max_urls, timeout, max_bytes):
    queue = list(dict.fromkeys(seed_sitemaps))
    seen_sitemaps = set()
    pages = []
    sitemap_reports = []
    while queue and len(seen_sitemaps) < max_sitemaps and len(pages) < max_urls:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            report = fetch_one_sitemap(sitemap_url, timeout, max_bytes)
        except Exception as exc:
            report = {"sitemap_url": sitemap_url, "status": "", "ok": False, "child_sitemaps": [], "pages": [], "error": repr(exc)}
        sitemap_reports.append({k: report[k] for k in ["sitemap_url", "status", "ok", "error"]})
        for child in report["child_sitemaps"]:
            normalized = normalize_url(child["url"], sitemap_url)
            if normalized and normalized not in seen_sitemaps:
                queue.append(normalized)
        for page in report["pages"]:
            pages.append(page)
            if len(pages) >= max_urls:
                break
    return pages, sitemap_reports


def parse_feed_urls(feed_url, timeout, max_bytes):
    try:
        status, final_url, headers, data = fetch_bytes(feed_url, timeout, max_bytes)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        root = ET.fromstring(data)
    except Exception:
        return []
    urls = []
    for elem in root.iter():
        tag = elem.tag.lower()
        if tag.endswith("link"):
            rel = elem.attrib.get("rel", "alternate").lower()
            href = elem.attrib.get("href")
            text = elem.text.strip() if elem.text else ""
            candidate = href or text
            if candidate and rel in {"alternate", ""}:
                normalized = normalize_url(candidate, feed_url)
                if normalized:
                    urls.append(normalized)
    return urls


def candidate_score(item):
    source_scores = {"feed": 100, "sitemap": 80, "link_depth1": 50, "homepage_link": 40}
    score = source_scores.get(item.get("source", ""), 10)
    lastmod = item.get("lastmod") or ""
    if lastmod:
        score += 10
        try:
            normalized = lastmod.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = max(0, (datetime.now(timezone.utc) - dt).days)
            score += max(0, 30 - min(30, age_days / 7))
        except Exception:
            pass
    path = urlparse(item["url"]).path.lower()
    if any(part in path for part in ["/docs/", "/reference/", "/tutorial/", "/guide/", "/article/", "/wiki/", "/publication/"]):
        score += 15
    if urlparse(item["url"]).query:
        score -= 10
    return score


def filter_candidates(raw_candidates, allowed_hosts, robots, include_prefixes, max_candidates):
    seen_url_hashes = set()
    accepted = []
    rejects = Counter()
    for item in raw_candidates:
        normalized = normalize_url(item.get("url", ""))
        if not normalized:
            rejects["normalize_failed"] += 1
            continue
        digest = url_hash(normalized)
        if digest in seen_url_hashes:
            rejects["duplicate_url"] += 1
            continue
        seen_url_hashes.add(digest)
        if not same_site(normalized, allowed_hosts):
            rejects["offsite"] += 1
            continue
        if not path_allowed(normalized, include_prefixes):
            rejects["path_prefix_filtered"] += 1
            continue
        if is_low_value_url(normalized):
            rejects["low_value"] += 1
            continue
        if not robots.can_fetch(USER_AGENT, normalized):
            rejects["robots_disallow"] += 1
            continue
        enriched = dict(item)
        enriched["url"] = normalized
        enriched["url_hash"] = digest
        enriched["score"] = candidate_score(enriched)
        accepted.append(enriched)
    accepted.sort(key=lambda row: (-row["score"], row["url"]))
    return accepted[:max_candidates], rejects


def extract_links_from_page(url, timeout, max_bytes):
    status, final_url, headers, data = fetch_bytes(url, timeout, max_bytes)
    if status != 200 or "html" not in header_get(headers, "Content-Type").lower():
        return []
    parser = LinkExtractor()
    parser.feed(data.decode("utf-8", errors="replace"))
    return [normalize_url(link, final_url) for link in parser.links]


def add_depth1_links(candidates, allowed_hosts, robots, include_prefixes, discovery_pages, timeout, max_bytes):
    raw = list(candidates)
    for item in candidates[:discovery_pages]:
        try:
            links = extract_links_from_page(item["url"], timeout, max_bytes)
        except Exception:
            continue
        for link in links:
            if link:
                raw.append({"url": link, "source": "link_depth1", "lastmod": "", "changefreq": "", "priority": ""})
    return filter_candidates(raw, allowed_hosts, robots, include_prefixes, len(raw))


def build_candidates(base_url, allowed_hosts, include_prefixes, max_sitemaps, max_candidates, link_discovery_pages, timeout, max_sitemap_bytes, max_page_bytes):
    robots, robots_url, robots_status, robot_sitemaps = robots_parser(base_url, timeout, max_page_bytes)
    homepage = {"status": None, "links": [], "feeds": [], "sitemaps": [], "final_url": base_url}
    try:
        homepage = discover_homepage_sources(base_url, timeout, max_page_bytes)
    except Exception:
        pass

    primary_sitemap_seeds = list(dict.fromkeys(robot_sitemaps + homepage["sitemaps"]))
    sitemap_reports = []
    sitemap_pages = []
    if primary_sitemap_seeds:
        sitemap_pages, sitemap_reports = discover_sitemap_pages(primary_sitemap_seeds, max_sitemaps, max_candidates, timeout, max_sitemap_bytes)
    if not sitemap_pages:
        common_sitemap_seeds = common_sitemap_urls(base_url)
        sitemap_pages, common_reports = discover_sitemap_pages(common_sitemap_seeds, max_sitemaps, max_candidates, timeout, max_sitemap_bytes)
        sitemap_reports.extend(common_reports)
    sitemap_seeds = primary_sitemap_seeds or common_sitemap_urls(base_url)

    feed_urls = []
    feed_sources = list(dict.fromkeys(homepage["feeds"] + common_feed_urls(base_url)))[:12]
    for feed in feed_sources:
        if robots.can_fetch(USER_AGENT, feed):
            feed_urls.extend(parse_feed_urls(feed, timeout, max_page_bytes))

    raw_candidates = []
    for page in sitemap_pages:
        raw_candidates.append({"url": page["url"], "source": "sitemap", "lastmod": page.get("lastmod", ""), "changefreq": page.get("changefreq", ""), "priority": page.get("priority", "")})
    for url in feed_urls:
        raw_candidates.append({"url": url, "source": "feed", "lastmod": "", "changefreq": "", "priority": ""})
    for url in homepage["links"]:
        raw_candidates.append({"url": url, "source": "homepage_link", "lastmod": "", "changefreq": "", "priority": ""})

    candidates, rejects = filter_candidates(raw_candidates, allowed_hosts, robots, include_prefixes, max_candidates)
    depth1_rejects = Counter()
    if link_discovery_pages > 0 and candidates:
        candidates, depth1_rejects = add_depth1_links(candidates, allowed_hosts, robots, include_prefixes, link_discovery_pages, timeout, max_page_bytes)
        candidates = candidates[:max_candidates]

    probe = {
        "robots_url": robots_url,
        "robots_status": robots_status,
        "robot_sitemaps": robot_sitemaps,
        "homepage_status": homepage["status"],
        "homepage_final_url": homepage["final_url"],
        "sitemap_seed_count": len(sitemap_seeds),
        "sitemap_reports": sitemap_reports,
        "raw_sitemap_urls": len(sitemap_pages),
        "raw_homepage_links": len(homepage["links"]),
        "raw_feed_urls": len(feed_urls),
        "candidate_urls": len(candidates),
        "reject_counts": dict(rejects),
        "depth1_reject_counts": dict(depth1_rejects),
    }
    return robots, candidates, probe


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


def fetch_and_extract(index, item, output_dir, timeout, max_page_bytes, save_html):
    url = item["url"]
    record = {
        "url": url,
        "source": item.get("source", ""),
        "lastmod": item.get("lastmod", ""),
        "url_hash": item.get("url_hash", ""),
        "score": item.get("score", 0),
        "ok": False,
    }
    try:
        status, final_url, headers, data = fetch_bytes(url, timeout, max_page_bytes)
        content_type = header_get(headers, "Content-Type")
        record.update({"status": status, "final_url": final_url, "content_type": content_type, "bytes": len(data)})
        if status != 200 or "html" not in content_type.lower():
            record["error"] = "non_html_or_non_200"
            return record, None
        html = data.decode("utf-8", errors="replace")
        title, text, extractor = extract_content(html, final_url)
        if save_html:
            html_dir = output_dir / "html"
            html_dir.mkdir(parents=True, exist_ok=True)
            html_path = html_dir / safe_name(final_url, index)
            html_path.write_bytes(data)
            record["html_file"] = str(html_path)
        record.update({"ok": True, "extractor": extractor, "title": title, "text_length": len(text)})
        extracted = {
            "url": final_url,
            "source": item.get("source", ""),
            "lastmod": item.get("lastmod", ""),
            "title": title,
            "text_length": len(text),
            "text": text,
            "extractor": extractor,
        }
        return record, extracted
    except Exception as exc:
        record["error"] = repr(exc)
        return record, None


def crawl_pages(candidates, output_dir, max_pages, max_workers, timeout, max_page_bytes, save_html, hamming_threshold):
    manifest = []
    extracted = []
    deduper = SimhashDeduper(hamming_threshold)
    dedupe_counts = Counter()
    target = candidates[:max_pages]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(fetch_and_extract, index, item, output_dir, timeout, max_page_bytes, save_html): item
            for index, item in enumerate(target, start=1)
        }
        for future in as_completed(future_map):
            record, extracted_item = future.result()
            if extracted_item:
                unique, reason, content_hash, simhash_hex = deduper.check_and_add(extracted_item["text"])
                record["content_hash"] = content_hash
                record["simhash"] = simhash_hex
                record["dedupe_status"] = reason
                if unique:
                    extracted_item["content_hash"] = content_hash
                    extracted_item["simhash"] = simhash_hex
                    extracted_item["text_preview"] = extracted_item["text"][:1000]
                    extracted.append(extracted_item)
                else:
                    record["ok"] = False
                dedupe_counts[reason] += 1
            manifest.append(record)
    manifest.sort(key=lambda row: row.get("url", ""))
    extracted.sort(key=lambda row: row.get("url", ""))
    return manifest, extracted, dedupe_counts


def write_jsonl(path, rows, drop_full_text=False):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            item = dict(row)
            if drop_full_text and "text" in item:
                item.pop("text")
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def write_candidates_csv(path, rows):
    fields = ["url", "url_hash", "source", "score", "lastmod", "changefreq", "priority"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_args():
    parser = argparse.ArgumentParser(description="Optimized live site crawler with robust sitemap discovery, trafilatura extraction, URL dedupe, and near-semantic dedupe")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--allowed-host", action="append", dest="allowed_hosts")
    parser.add_argument("--include-path-prefix", action="append", dest="include_prefixes")
    parser.add_argument("--output-dir", default="data/optimized_live_crawl")
    parser.add_argument("--max-sitemaps", type=int, default=50)
    parser.add_argument("--max-candidates", type=int, default=1000)
    parser.add_argument("--link-discovery-pages", type=int, default=5)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--max-page-bytes", type=int, default=2_000_000)
    parser.add_argument("--max-sitemap-bytes", type=int, default=50_000_000)
    parser.add_argument("--semantic-hamming-threshold", type=int, default=4)
    parser.add_argument("--save-html", action="store_true")
    parser.add_argument("--write-full-text", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    base = normalize_url(args.base_url)
    if not base:
        raise ValueError("Invalid base URL")
    base_host = urlparse(base).hostname.lower()
    allowed_hosts = set(args.allowed_hosts or [base_host])
    allowed_hosts.add(base_host)
    include_prefixes = args.include_prefixes or []
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    robots, candidates, probe = build_candidates(
        base,
        allowed_hosts,
        include_prefixes,
        args.max_sitemaps,
        args.max_candidates,
        args.link_discovery_pages,
        args.timeout,
        args.max_sitemap_bytes,
        args.max_page_bytes,
    )
    write_candidates_csv(output_dir / "candidates.csv", candidates)
    manifest, extracted, dedupe_counts = crawl_pages(
        candidates,
        output_dir,
        args.max_pages,
        args.max_workers,
        args.timeout,
        args.max_page_bytes,
        args.save_html,
        args.semantic_hamming_threshold,
    )
    write_jsonl(output_dir / "manifest.jsonl", manifest)
    write_jsonl(output_dir / "extracted_text.jsonl", extracted, drop_full_text=not args.write_full_text)
    if args.write_full_text:
        write_jsonl(output_dir / "extracted_full_text.jsonl", extracted)

    ok_records = [row for row in manifest if row.get("ok")]
    summary = {
        "base_url": base,
        "allowed_hosts": sorted(allowed_hosts),
        "include_path_prefixes": include_prefixes,
        "extractor": "trafilatura" if trafilatura is not None else "fallback",
        "probe": probe,
        "candidates": len(candidates),
        "requested_pages": min(len(candidates), args.max_pages),
        "unique_downloaded_pages": len(ok_records),
        "manifest_records": len(manifest),
        "dedupe_counts": dict(dedupe_counts),
        "elapsed_seconds": round(time.time() - started, 2),
        "files": {
            "candidates": str(output_dir / "candidates.csv"),
            "manifest": str(output_dir / "manifest.jsonl"),
            "extracted_text": str(output_dir / "extracted_text.jsonl"),
            "html_dir": str(output_dir / "html"),
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
