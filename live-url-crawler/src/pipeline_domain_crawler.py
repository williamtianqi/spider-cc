#!/usr/bin/env python3
import argparse
import html as html_module
import json
import os
import re
import threading
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from pathlib import Path

import domain_link_crawler as base


def write_jsonl_row(handle, row):
    if handle is not None:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def flush_handles(*handles):
    for handle in handles:
        if handle is not None:
            handle.flush()


HREF_RE = re.compile(r'''(?i)\bhref\s*=\s*["']([^"'#\s>]+)["']''')
TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript|svg|canvas|template)[^>]*>.*?</\1>")
TAG_RE = re.compile(r"(?s)<[^>]+>")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_RE = re.compile(r"\n\s*\n+")
CHARSET_RE = re.compile(r'''(?i)charset\s*=\s*["']?([A-Za-z0-9._-]+)''')


def decode_html_bytes(data, headers):
    candidates = []
    content_type = base.header_get(headers, "Content-Type")
    header_match = CHARSET_RE.search(content_type)
    if header_match:
        candidates.append(header_match.group(1))
    head = data[:4096].decode("ascii", errors="ignore")
    meta_match = CHARSET_RE.search(head)
    if meta_match:
        candidates.append(meta_match.group(1))
    candidates.extend(["utf-8", "gb18030", "big5", "shift_jis", "euc-kr", "latin-1"])
    seen = set()
    for charset in candidates:
        charset = charset.lower()
        if charset in seen:
            continue
        seen.add(charset)
        try:
            return data.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def fast_extract_content(html, url):
    fallback = base.FallbackTextExtractor()
    fallback.feed(html)
    title, text = fallback.result()
    return title, text, "fast_fallback_inline"


def regex_extract_content(html, url):
    title_match = TITLE_RE.search(html)
    title = html_module.unescape(TAG_RE.sub(" ", title_match.group(1))).strip() if title_match else ""
    body = SCRIPT_STYLE_RE.sub(" ", html)
    body = re.sub(r"(?i)</(p|div|section|article|li|tr|h[1-6]|br)>\s*", "\n", body)
    text = TAG_RE.sub(" ", body)
    text = html_module.unescape(text)
    text = SPACE_RE.sub(" ", text)
    text = BLANK_RE.sub("\n", text).strip()
    return title, text, "regex_inline"


def regex_extract_page_links(html, base_url):
    links = []
    feeds = []
    sitemaps = []
    canonical = ""
    for href in HREF_RE.findall(html):
        normalized = base.normalize_url(href, base_url)
        if not normalized:
            continue
        lower = normalized.lower()
        is_feed = "rss" in lower or "atom" in lower or lower.endswith("/feed") or lower.endswith("/feed.xml")
        is_sitemap = "sitemap" in lower
        if "/_static/" in lower:
            continue
        if lower.endswith(".xml") and not (is_feed or is_sitemap):
            continue
        links.append(normalized)
        if is_feed:
            feeds.append(normalized)
        if is_sitemap:
            sitemaps.append(normalized)
    return {"links": links, "feeds": feeds, "sitemaps": sitemaps, "canonical": canonical}


def build_extracted(item, url, title, text, extractor, write_full_text):
    c_hash = base.content_hash(text)
    extracted = {
        "url": url,
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
    return extracted


def write_extracted_record(record, extracted, content_hashes, counters, pages_handle, manifest_handle):
    record.update({"ok": True, "title": extracted["title"], "text_length": extracted["text_length"], "content_hash": extracted["content_hash"], "extractor": extracted["extractor"]})
    if extracted["content_hash"] in content_hashes:
        record["content_duplicate"] = True
        counters["duplicate_content"] += 1
    else:
        content_hashes.add(extracted["content_hash"])
        counters["unique_text"] += 1
        write_jsonl_row(pages_handle, extracted)
    write_jsonl_row(manifest_handle, record)


def fetch_sitemap_worker(item, timeout, max_sitemap_bytes):
    record = {
        "url": item["url"],
        "url_hash": item["url_hash"],
        "source": item["source"],
        "depth": item["depth"],
        "from_url": item["from_url"],
        "ok": False,
    }
    try:
        status, final_url, headers, data = base.fetch_bytes(item["url"], timeout, max_sitemap_bytes)
        if data[:2] == b"\x1f\x8b":
            data = base.gzip.decompress(data)
        record.update({"status": status, "final_url": final_url, "bytes": len(data)})
        if status != 200 or not base.looks_like_xml(data, headers):
            record["error"] = "sitemap_not_xml"
            return {"record": record, "extract_input": None, "outlinks": []}
        child_sitemaps, pages = base.parse_sitemap_xml(data)
        outlinks = []
        for child in child_sitemaps:
            child_url = base.normalize_url(child["url"], final_url, allow_static=True)
            if child_url:
                outlinks.append({"url": child_url, "source": "sitemap_link", "from_url": final_url, "depth": item["depth"] + 1})
        for page in pages:
            page_url = base.normalize_url(page["url"], final_url)
            if page_url:
                outlinks.append({"url": page_url, "source": "sitemap_link_page", "from_url": final_url, "depth": item["depth"] + 1})
        record.update({"ok": True, "sitemap_child_count": len(child_sitemaps), "sitemap_page_count": len(pages)})
        return {"record": record, "extract_input": None, "outlinks": outlinks}
    except Exception as exc:
        record["error"] = repr(exc)
        return {"record": record, "extract_input": None, "outlinks": []}


def fetch_feed_worker(item, timeout, max_page_bytes):
    record = {
        "url": item["url"],
        "url_hash": item["url_hash"],
        "source": item["source"],
        "depth": item["depth"],
        "from_url": item["from_url"],
        "ok": False,
    }
    try:
        status, final_url, headers, data = base.fetch_bytes(item["url"], timeout, max_page_bytes)
        if data[:2] == b"\x1f\x8b":
            data = base.gzip.decompress(data)
        record.update({"status": status, "final_url": final_url, "bytes": len(data)})
        if status != 200 or not base.looks_like_xml(data, headers):
            record["error"] = "feed_not_xml"
            return {"record": record, "extract_input": None, "outlinks": []}
        urls = base.parse_feed_xml(data, final_url)
        outlinks = [{"url": url, "source": "feed_item", "from_url": final_url, "depth": item["depth"] + 1} for url in urls]
        record.update({"ok": True, "feed_item_count": len(urls)})
        return {"record": record, "extract_input": None, "outlinks": outlinks}
    except Exception as exc:
        record["error"] = repr(exc)
        return {"record": record, "extract_input": None, "outlinks": []}


def fetch_worker(item, timeout, max_page_bytes, max_sitemap_bytes, host_semaphores, host_lock, max_host_workers, extract_mode, link_mode, write_full_text):
    semaphore = base.get_host_semaphore(item["url"], host_semaphores, host_lock, max_host_workers)
    if semaphore:
        semaphore.acquire()
    try:
        if item["source"] == "sitemap_link":
            return fetch_sitemap_worker(item, timeout, max_sitemap_bytes)
        if item["source"] == "feed":
            return fetch_feed_worker(item, timeout, max_page_bytes)
        record = {
            "url": item["url"],
            "url_hash": item["url_hash"],
            "source": item["source"],
            "depth": item["depth"],
            "from_url": item["from_url"],
            "ok": False,
        }
        try:
            status, final_url, headers, data = base.fetch_bytes(item["url"], timeout, max_page_bytes)
            content_type = base.header_get(headers, "Content-Type")
            record.update({"status": status, "final_url": final_url, "content_type": content_type, "bytes": len(data)})
            if status != 200 or "html" not in content_type.lower():
                record["error"] = "non_html_or_non_200"
                return {"record": record, "extract_input": None, "outlinks": []}
            html = decode_html_bytes(data, headers)
            page_links = regex_extract_page_links(html, final_url) if link_mode == "regex" else base.extract_page_links(html, final_url)
            record["canonical"] = page_links["canonical"]
            outlinks = [{"url": link, "source": "page_link", "from_url": final_url, "depth": item["depth"] + 1} for link in page_links["links"]]
            outlinks.extend({"url": feed, "source": "feed", "from_url": final_url, "depth": item["depth"] + 1} for feed in page_links["feeds"])
            outlinks.extend({"url": sitemap, "source": "sitemap_link", "from_url": final_url, "depth": item["depth"] + 1} for sitemap in page_links["sitemaps"])
            if extract_mode in {"fast-inline", "regex-inline"}:
                if extract_mode == "regex-inline":
                    title, text, extractor = regex_extract_content(html, final_url)
                else:
                    title, text, extractor = fast_extract_content(html, final_url)
                extracted = build_extracted(item, final_url, title, text, extractor, write_full_text)
                return {"record": record, "extracted": extracted, "extract_input": None, "outlinks": outlinks}
            return {"record": record, "extracted": None, "extract_input": {"html": html, "url": final_url, "item": item}, "outlinks": outlinks}
        except Exception as exc:
            record["error"] = repr(exc)
            return {"record": record, "extract_input": None, "outlinks": []}
    finally:
        if semaphore:
            semaphore.release()


def extract_worker(payload, write_full_text):
    item = payload["item"]
    url = payload["url"]
    title, text, extractor = base.extract_content(payload["html"], url)
    return build_extracted(item, url, title, text, extractor, write_full_text)


def log_line(log_path, message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def progress_snapshot(args, started, scheduled, fetched, ok, unique_text, duplicate_content, queue, discovered_count, link_count, reject_counts, in_fetch, in_extract):
    elapsed = max(0.001, time.time() - started)
    return {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(elapsed, 2),
        "fetch_pages_per_second": round(fetched / elapsed, 4),
        "scheduled_pages": scheduled,
        "fetched_pages": fetched,
        "ok_manifest_records": ok,
        "unique_extracted_text_records": unique_text,
        "duplicate_content_records": duplicate_content,
        "discovered_urls": discovered_count,
        "remaining_frontier": len(queue),
        "link_edges": link_count,
        "in_flight_fetch": in_fetch,
        "in_flight_extract": in_extract,
        "reject_counts": dict(reject_counts),
        "limits": {
            "max_pages": args.max_pages,
            "max_discovered": args.max_discovered,
            "max_depth": args.max_depth,
            "fetch_workers": args.fetch_workers,
            "extract_workers": args.extract_workers,
            "max_host_workers": args.max_host_workers,
            "max_in_flight_fetch": args.max_in_flight_fetch,
            "max_in_flight_extract": args.max_in_flight_extract,
        },
    }


def write_progress(output_dir, snapshot):
    (output_dir / "progress.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_resumable_run(output_dir):
    return (output_dir / "manifest.jsonl").exists() and not (output_dir / "summary.json").exists()


def is_locked_by_live_process(output_dir):
    lock_path = output_dir / "pid.lock"
    if not lock_path.exists():
        return False
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def count_jsonl_lines(path):
    path = Path(path)
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def load_hash_set(path, field):
    values = set()
    path = Path(path)
    if not path.exists():
        return values
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = row.get(field)
            if value:
                values.add(value)
    return values


def load_manifest_stats(path):
    fetched = 0
    ok = 0
    duplicate_content = 0
    path = Path(path)
    if not path.exists():
        return {"fetched": 0, "ok": 0, "duplicate_content": 0}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            fetched += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("ok"):
                ok += 1
            if row.get("content_duplicate"):
                duplicate_content += 1
    return {"fetched": fetched, "ok": ok, "duplicate_content": duplicate_content}


def load_frontier_checkpoint(path):
    queue = deque()
    path = Path(path)
    if not path.exists():
        return queue
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            queue.append(row)
    return queue


def write_frontier_checkpoint(output_dir, queue):
    tmp_path = output_dir / "frontier_checkpoint.jsonl.tmp"
    final_path = output_dir / "frontier_checkpoint.jsonl"
    with tmp_path.open("w", encoding="utf-8") as f:
        for row in queue:
            write_jsonl_row(f, row)
    os.replace(tmp_path, final_path)


def enqueue(queue, seen, discovered_handle, counters, url, source, depth, from_url, allowed_hosts, allowed_domains, include_subdomains, robots_cache, timeout, max_page_bytes, max_discovered, check_robots):
    if counters["discovered"] >= max_discovered:
        counters["reject_counts"]["max_discovered_reached"] += 1
        return
    normalized = base.normalize_url(url, from_url)
    if not normalized:
        counters["reject_counts"]["normalize_failed"] += 1
        return
    digest = base.url_hash(normalized)
    if digest in seen:
        counters["reject_counts"]["duplicate_url"] += 1
        return
    if not base.in_scope(normalized, allowed_hosts, allowed_domains, include_subdomains):
        counters["reject_counts"]["out_of_scope"] += 1
        return
    if check_robots and not base.can_fetch(normalized, robots_cache, timeout, max_page_bytes):
        counters["reject_counts"]["robots_disallow"] += 1
        return
    row = {"url": normalized, "source": source, "depth": depth, "from_url": from_url or "", "url_hash": digest}
    seen.add(digest)
    queue.append(row)
    counters["discovered"] += 1
    counters["reject_counts"]["queued"] += 1
    write_jsonl_row(discovered_handle, row)


def parse_args():
    parser = argparse.ArgumentParser(description="Single-node master-worker pipeline crawler with JSONL output")
    parser.add_argument("--seed-url", required=True)
    parser.add_argument("--allowed-host", action="append", dest="allowed_hosts")
    parser.add_argument("--allowed-domain", action="append", dest="allowed_domains")
    parser.add_argument("--include-subdomains", action="store_true")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--max-discovered", type=int, default=200000)
    parser.add_argument("--max-depth", type=int, default=20)
    parser.add_argument("--max-sitemaps", type=int, default=500)
    parser.add_argument("--fetch-workers", type=int, default=64)
    parser.add_argument("--extract-workers", type=int, default=8)
    parser.add_argument("--max-host-workers", type=int, default=32)
    parser.add_argument("--max-in-flight-fetch", type=int, default=512)
    parser.add_argument("--max-in-flight-extract", type=int, default=256)
    parser.add_argument("--progress-interval", type=int, default=100)
    parser.add_argument("--extract-mode", choices=["trafilatura", "fast-inline", "regex-inline"], default="trafilatura")
    parser.add_argument("--link-mode", choices=["htmlparser", "regex"], default="htmlparser")
    parser.add_argument("--robots-check-stage", choices=["enqueue", "fetch"], default="enqueue")
    parser.add_argument("--no-write-links", action="store_true")
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-page-bytes", type=int, default=2000000)
    parser.add_argument("--max-sitemap-bytes", type=int, default=50000000)
    parser.add_argument("--write-full-text", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume from an existing incomplete run in --output-dir using the periodic frontier checkpoint, discovered_urls.jsonl and pages.jsonl instead of restarting from the seed URL.")
    return parser.parse_args()


def main():
    args = parse_args()
    seed_url = base.normalize_url(args.seed_url)
    if not seed_url:
        raise ValueError("Invalid seed URL")
    seed_host = base.host_of(seed_url)
    allowed_hosts = set(args.allowed_hosts or [seed_host])
    allowed_domains = set(args.allowed_domains or [])

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    resume = args.resume and is_resumable_run(output_dir)
    if args.resume and is_locked_by_live_process(output_dir):
        raise RuntimeError(f"output_dir {output_dir} is locked by a live process (pid.lock); refusing to resume concurrently")

    log_path = output_dir / "crawl.log"
    if not resume:
        log_path.write_text("", encoding="utf-8")
    started = time.time()
    robots_cache = {}
    host_semaphores = {}
    host_lock = threading.Lock()

    if resume:
        seen = load_hash_set(output_dir / "discovered_urls.jsonl", "url_hash")
        content_hashes = load_hash_set(output_dir / "pages.jsonl", "content_hash")
        queue = load_frontier_checkpoint(output_dir / "frontier_checkpoint.jsonl")
        manifest_stats = load_manifest_stats(output_dir / "manifest.jsonl")
        counters = {
            "discovered": len(seen),
            "scheduled": manifest_stats["fetched"],
            "fetched": manifest_stats["fetched"],
            "ok": manifest_stats["ok"],
            "unique_text": len(content_hashes),
            "duplicate_content": manifest_stats["duplicate_content"],
            "links": count_jsonl_lines(output_dir / "links.jsonl") if not args.no_write_links else 0,
            "reject_counts": Counter(),
        }
    else:
        queue = deque()
        seen = set()
        content_hashes = set()
        counters = {
            "discovered": 0,
            "scheduled": 0,
            "fetched": 0,
            "ok": 0,
            "unique_text": 0,
            "duplicate_content": 0,
            "links": 0,
            "reject_counts": Counter(),
        }

    file_mode = "a" if resume else "w"
    discovered_f = (output_dir / "discovered_urls.jsonl").open(file_mode, encoding="utf-8")
    links_f = None if args.no_write_links else (output_dir / "links.jsonl").open(file_mode, encoding="utf-8")
    manifest_f = (output_dir / "manifest.jsonl").open(file_mode, encoding="utf-8")
    pages_f = (output_dir / "pages.jsonl").open(file_mode, encoding="utf-8")
    sitemap_f = (output_dir / "sitemap_reports.jsonl").open(file_mode, encoding="utf-8")

    pid_lock_path = output_dir / "pid.lock"
    pid_lock_path.write_text(str(os.getpid()), encoding="utf-8")

    sitemap_pages, sitemap_reports = [], []
    try:
        check_robots_on_enqueue = args.robots_check_stage == "enqueue"
        if resume:
            log_line(log_path, f"resume seed={seed_url} loaded_seen={len(seen)} loaded_content_hashes={len(content_hashes)} loaded_frontier={len(queue)} prior_fetched={counters['fetched']} prior_ok={counters['ok']}")
        else:
            log_line(log_path, f"start seed={seed_url} max_pages={args.max_pages} fetch_workers={args.fetch_workers} extract_workers={args.extract_workers} max_host_workers={args.max_host_workers} extract_mode={args.extract_mode} link_mode={args.link_mode} robots_check_stage={args.robots_check_stage} write_links={not args.no_write_links}")
            sitemap_pages, sitemap_reports = base.discover_sitemaps(seed_url, allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes, args.max_sitemap_bytes, args.max_sitemaps)
            for report in sitemap_reports:
                write_jsonl_row(sitemap_f, report)
            log_line(log_path, f"sitemap_discovery pages={len(sitemap_pages)} reports={len(sitemap_reports)}")
            enqueue(queue, seen, discovered_f, counters, seed_url, "seed", 0, "", allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes, args.max_discovered, check_robots_on_enqueue)
            for page in sitemap_pages:
                enqueue(queue, seen, discovered_f, counters, page["url"], "sitemap", 0, "", allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes, args.max_discovered, check_robots_on_enqueue)

        fetch_futures = {}
        extract_futures = {}
        next_progress_at = max(1, args.progress_interval)
        with ThreadPoolExecutor(max_workers=args.fetch_workers) as fetch_pool, ProcessPoolExecutor(max_workers=args.extract_workers) as extract_pool:
            while queue or fetch_futures or extract_futures:
                while queue and counters["scheduled"] < args.max_pages and len(fetch_futures) < args.max_in_flight_fetch:
                    item = queue.popleft()
                    if item["depth"] > args.max_depth:
                        counters["reject_counts"]["depth_exceeded"] += 1
                        continue
                    if args.robots_check_stage == "fetch" and not base.can_fetch(item["url"], robots_cache, args.timeout, args.max_page_bytes):
                        counters["reject_counts"]["robots_disallow"] += 1
                        continue
                    future = fetch_pool.submit(fetch_worker, item, args.timeout, args.max_page_bytes, args.max_sitemap_bytes, host_semaphores, host_lock, args.max_host_workers, args.extract_mode, args.link_mode, args.write_full_text)
                    fetch_futures[future] = item
                    counters["scheduled"] += 1
                if not fetch_futures and not extract_futures:
                    break
                pending = set(fetch_futures) | set(extract_futures)
                done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    if future in fetch_futures:
                        item = fetch_futures.pop(future)
                        result = future.result()
                        record = result["record"]
                        counters["fetched"] += 1
                        if result.get("extracted") or result.get("extract_input"):
                            counters["ok"] += 1
                        if result.get("extracted"):
                            write_extracted_record(record, result["extracted"], content_hashes, counters, pages_f, manifest_f)
                        elif not result.get("extract_input"):
                            write_jsonl_row(manifest_f, record)
                        else:
                            while len(extract_futures) >= args.max_in_flight_extract:
                                extract_done, _ = wait(set(extract_futures), timeout=0.2, return_when=FIRST_COMPLETED)
                                for extract_future in extract_done:
                                    original_record = extract_futures.pop(extract_future)
                                    extracted = extract_future.result()
                                    write_extracted_record(original_record, extracted, content_hashes, counters, pages_f, manifest_f)
                            extract_future = extract_pool.submit(extract_worker, result["extract_input"], args.write_full_text)
                            extract_futures[extract_future] = record
                        for outlink in result["outlinks"]:
                            if links_f is not None:
                                write_jsonl_row(links_f, {"from_url": outlink["from_url"], "to_url": outlink["url"], "source": outlink["source"]})
                            counters["links"] += 1
                            if outlink["depth"] <= args.max_depth:
                                enqueue(queue, seen, discovered_f, counters, outlink["url"], outlink["source"], outlink["depth"], outlink["from_url"], allowed_hosts, allowed_domains, args.include_subdomains, robots_cache, args.timeout, args.max_page_bytes, args.max_discovered, check_robots_on_enqueue)
                    else:
                        record = extract_futures.pop(future)
                        extracted = future.result()
                        write_extracted_record(record, extracted, content_hashes, counters, pages_f, manifest_f)
                if counters["fetched"] >= next_progress_at:
                    flush_handles(discovered_f, links_f, manifest_f, pages_f, sitemap_f)
                    write_frontier_checkpoint(output_dir, queue)
                    snapshot = progress_snapshot(args, started, counters["scheduled"], counters["fetched"], counters["ok"], counters["unique_text"], counters["duplicate_content"], queue, counters["discovered"], counters["links"], counters["reject_counts"], len(fetch_futures), len(extract_futures))
                    write_progress(output_dir, snapshot)
                    log_line(log_path, f"progress fetched={snapshot['fetched_pages']} rate={snapshot['fetch_pages_per_second']}/s scheduled={snapshot['scheduled_pages']} discovered={snapshot['discovered_urls']} frontier={snapshot['remaining_frontier']} in_fetch={snapshot['in_flight_fetch']} in_extract={snapshot['in_flight_extract']} unique_text={snapshot['unique_extracted_text_records']}")
                    next_progress_at += max(1, args.progress_interval)

        frontier_path = output_dir / "frontier_remaining.jsonl"
        with frontier_path.open("w", encoding="utf-8") as f:
            for row in queue:
                write_jsonl_row(f, row)
        flush_handles(discovered_f, links_f, manifest_f, pages_f, sitemap_f)
        final_snapshot = progress_snapshot(args, started, counters["scheduled"], counters["fetched"], counters["ok"], counters["unique_text"], counters["duplicate_content"], queue, counters["discovered"], counters["links"], counters["reject_counts"], 0, 0)
        write_progress(output_dir, final_snapshot)
        log_line(log_path, f"finished fetched={final_snapshot['fetched_pages']} rate={final_snapshot['fetch_pages_per_second']}/s discovered={final_snapshot['discovered_urls']} frontier={final_snapshot['remaining_frontier']} unique_text={final_snapshot['unique_extracted_text_records']}")
        stopped_reason = "frontier_exhausted"
        if len(queue) > 0 and counters["scheduled"] >= args.max_pages:
            stopped_reason = "max_pages_reached"
        elif len(queue) > 0 and counters["discovered"] >= args.max_discovered:
            stopped_reason = "max_discovered_reached"
        site_crawl_complete = len(queue) == 0 and stopped_reason == "frontier_exhausted"
        summary = {
            "seed_url": seed_url,
            "allowed_hosts": sorted(allowed_hosts),
            "allowed_domains": sorted(allowed_domains),
            "include_subdomains": args.include_subdomains,
            "mode": "pipeline_master_fetcher_extractor",
            "resumed_from_checkpoint": resume,
            "extract_mode": args.extract_mode,
            "link_mode": args.link_mode,
            "robots_check_stage": args.robots_check_stage,
            "extractor": args.extract_mode if args.extract_mode in {"fast-inline", "regex-inline"} else ("trafilatura" if base.trafilatura is not None else "fallback"),
            "sitemap_pages": len(sitemap_pages),
            "sitemap_reports": len(sitemap_reports),
            "discovered_urls": counters["discovered"],
            "scheduled_pages": counters["scheduled"],
            "fetched_pages": counters["fetched"],
            "ok_manifest_records": counters["ok"],
            "unique_text_pages": counters["unique_text"],
            "duplicate_content_pages": counters["duplicate_content"],
            "remaining_frontier": len(queue),
            "site_crawl_complete": site_crawl_complete,
            "stopped_reason": stopped_reason,
            "link_edges": counters["links"],
            "reject_counts": dict(counters["reject_counts"]),
            "elapsed_seconds": round(time.time() - started, 2),
            "files": {
                "pages": str(output_dir / "pages.jsonl"),
                "manifest": str(output_dir / "manifest.jsonl"),
                "discovered_urls": str(output_dir / "discovered_urls.jsonl"),
                "links": str(output_dir / "links.jsonl") if links_f is not None else "",
                "sitemap_reports": str(output_dir / "sitemap_reports.jsonl"),
                "frontier_remaining": str(frontier_path),
                "progress": str(output_dir / "progress.json"),
                "log": str(log_path),
            },
        }
        (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        checkpoint_path = output_dir / "frontier_checkpoint.jsonl"
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        for handle in [discovered_f, links_f, manifest_f, pages_f, sitemap_f]:
            if handle is not None:
                handle.close()
        if pid_lock_path.exists():
            pid_lock_path.unlink()


if __name__ == "__main__":
    main()
