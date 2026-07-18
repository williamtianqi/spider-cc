#!/usr/bin/env python3
"""
Export per-domain URL lists from the Common Crawl CDX index so the live
crawler frontier can be seeded with every URL Common Crawl already knows
about, instead of rediscovering pages through BFS link expansion only.

Why this exists (coverage + source-level dedup):
  - Coverage: deep pages with no inbound links and pages missing from
    sitemaps are invisible to BFS. The CC index lists them directly.
  - Source dedup: every CDX record carries the content digest
    (WARC-Payload-Digest). URLs sharing a digest are exact-content
    duplicates, so only the first URL per digest is exported and the
    crawler never wastes a request on the mirror copies.

Pipeline position:
  common_crawl_site_discovery.py  ->  cc_index_url_seeder.py  ->  pipeline_domain_crawler.py
        (seed sites TSV)                (per-domain URL JSONL)      (--seed-urls-file)

Examples:
  # Export URL lists for every site produced by discovery
  python3 src/cc_index_url_seeder.py \
    --crawl-id CC-MAIN-2025-08 \
    --seeds-tsv cc_seed_sites.tsv \
    --output-dir cc_index_urls \
    --html-only \
    --processed-domains-file cc_index_urls/processed_domains.txt

  # Then crawl one site with the exported list as extra frontier seeds
  python3 src/pipeline_domain_crawler.py \
    --seed-url https://example.com/ \
    --allowed-domain example.com --include-subdomains \
    --seed-urls-file cc_index_urls/example.com.jsonl \
    --output-dir data/runs/example.com
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import domain_link_crawler as base

INDEX_BASE_URL = "https://index.commoncrawl.org/"
USER_AGENT = "cc-index-url-seeder/0.1"


def emit_progress(row):
    row = dict(row)
    row["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(json.dumps(row, ensure_ascii=False), file=sys.stderr, flush=True)


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "site"


def index_endpoint(crawl_id):
    return f"{INDEX_BASE_URL}{crawl_id}-index"


def fetch_index_lines(endpoint, params, timeout, max_retries=4):
    url = f"{endpoint}?{urlencode(params)}"
    last_error = None
    for attempt in range(max_retries):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace").splitlines()
        except HTTPError as exc:
            if exc.code == 404:
                return []
            last_error = exc
            time.sleep(min(60, 5 * (2 ** attempt)))
        except Exception as exc:
            last_error = exc
            time.sleep(min(60, 5 * (2 ** attempt)))
    raise last_error


def count_index_pages(endpoint, domain, timeout):
    lines = fetch_index_lines(endpoint, {"url": domain, "matchType": "domain", "output": "json", "showNumPages": "true"}, timeout)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "pages" in row:
            return int(row["pages"])
    return 0


def export_domain_urls(domain, output_name, args):
    started = time.time()
    endpoint = index_endpoint(args.crawl_id)
    counters = Counter()
    output_path = Path(args.output_dir) / f"{output_name}.jsonl"
    seen_digests = set()
    seen_url_hashes = set()
    exported = 0
    capped = False
    try:
        pages = count_index_pages(endpoint, domain, args.timeout)
        counters["index_pages"] = pages
        with output_path.open("w", encoding="utf-8") as out_f:
            for page in range(pages):
                if exported >= args.max_urls_per_domain:
                    capped = True
                    break
                params = {
                    "url": domain,
                    "matchType": "domain",
                    "output": "json",
                    "fl": "url,digest,mime,status",
                    "filter": "status:200",
                    "page": page,
                }
                lines = fetch_index_lines(endpoint, params, args.timeout)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        counters["bad_index_lines"] += 1
                        continue
                    raw_url = row.get("url", "")
                    counters["index_records"] += 1
                    mime = row.get("mime", "")
                    if args.html_only and mime and "html" not in mime.lower():
                        counters["skipped_non_html"] += 1
                        continue
                    normalized = base.normalize_url(raw_url)
                    if not normalized:
                        counters["skipped_normalize_failed"] += 1
                        continue
                    url_digest = base.url_hash(normalized)
                    if url_digest in seen_url_hashes:
                        counters["skipped_duplicate_url"] += 1
                        continue
                    content_digest = row.get("digest", "")
                    if content_digest and content_digest in seen_digests:
                        counters["skipped_duplicate_digest"] += 1
                        continue
                    seen_url_hashes.add(url_digest)
                    if content_digest:
                        seen_digests.add(content_digest)
                    out_f.write(json.dumps({"url": normalized, "digest": content_digest, "mime": mime, "status": row.get("status", ""), "source": "cc_index"}, ensure_ascii=False) + "\n")
                    exported += 1
                    if exported >= args.max_urls_per_domain:
                        capped = True
                        break
                if args.page_delay > 0:
                    time.sleep(args.page_delay)
        return {
            "domain": domain,
            "output_name": output_name,
            "output_path": str(output_path),
            "ok": True,
            "capped": capped,
            "exported_urls": exported,
            "counters": dict(counters),
            "elapsed_seconds": round(time.time() - started, 2),
        }
    except Exception as exc:
        return {
            "domain": domain,
            "output_name": output_name,
            "output_path": str(output_path),
            "ok": False,
            "error": repr(exc),
            "exported_urls": exported,
            "counters": dict(counters),
            "elapsed_seconds": round(time.time() - started, 2),
        }


def load_domains_from_seeds_tsv(path):
    domains = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            scope = parts[1].strip()
            output_name = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else safe_name(scope)
            if scope:
                domains.append((scope, output_name))
    return domains


def load_processed_domains(path):
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    with p.open("r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def parse_args():
    parser = argparse.ArgumentParser(description="Export per-domain URL+digest lists from the Common Crawl CDX index for frontier seeding")
    parser.add_argument("--crawl-id", default="CC-MAIN-2025-08")
    parser.add_argument("--domain", action="append", dest="domains", help="Registrable domain to export (repeatable)")
    parser.add_argument("--seeds-tsv", help="Seed sites TSV from common_crawl_site_discovery.py (seed_url\\tscope\\toutput_name)")
    parser.add_argument("--output-dir", default="cc_index_urls")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent domains; keep low, index.commoncrawl.org rate-limits aggressively")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--page-delay", type=float, default=0.5, help="Seconds to sleep between index page requests per domain")
    parser.add_argument("--max-urls-per-domain", type=int, default=500000)
    parser.add_argument("--html-only", action="store_true", help="Keep only records whose index mime contains html")
    parser.add_argument(
        "--processed-domains-file",
        help="Persisted registry of domains already exported. Domains listed "
        "are skipped so re-runs and crash restarts never re-query the index "
        "for the same domain; successfully exported domains are appended.",
    )
    parser.add_argument("--manifest-jsonl", default="cc_index_url_seeder_manifest.jsonl")
    parser.add_argument("--summary-json", default="cc_index_url_seeder_summary.json")
    return parser.parse_args()


def main():
    args = parse_args()
    started = time.time()
    targets = []
    if args.seeds_tsv:
        targets.extend(load_domains_from_seeds_tsv(args.seeds_tsv))
    for domain in args.domains or []:
        targets.append((domain, safe_name(domain)))
    deduped = []
    seen_targets = set()
    for domain, output_name in targets:
        if domain in seen_targets:
            continue
        seen_targets.add(domain)
        deduped.append((domain, output_name))
    targets = deduped
    if not targets:
        raise SystemExit("no domains to export; pass --domain and/or --seeds-tsv")

    processed_domains = load_processed_domains(args.processed_domains_file)
    skipped_processed = 0
    if processed_domains:
        before = len(targets)
        targets = [(d, n) for d, n in targets if d not in processed_domains]
        skipped_processed = before - len(targets)

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    processed_f = Path(args.processed_domains_file).open("a", encoding="utf-8") if args.processed_domains_file else None
    emit_progress({"stage": "seeder_start", "crawl_id": args.crawl_id, "domains": len(targets), "skipped_already_processed": skipped_processed})

    totals = Counter()
    results = []
    next_index = 0
    with Path(args.manifest_jsonl).open("w", encoding="utf-8") as manifest_f:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            while next_index < len(targets) and len(futures) < args.workers:
                domain, output_name = targets[next_index]
                futures[pool.submit(export_domain_urls, domain, output_name, args)] = domain
                next_index += 1
            while futures:
                done, _ = wait(set(futures), timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    futures.pop(future)
                    result = future.result()
                    results.append(result)
                    manifest_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                    manifest_f.flush()
                    if result["ok"]:
                        totals["ok_domains"] += 1
                        totals["exported_urls"] += result["exported_urls"]
                        if result.get("capped"):
                            totals["capped_domains"] += 1
                        if processed_f is not None:
                            processed_f.write(result["domain"] + "\n")
                            processed_f.flush()
                    else:
                        totals["failed_domains"] += 1
                    for key, value in result.get("counters", {}).items():
                        totals[key] += value
                    emit_progress({"stage": "domain_done", "domain": result["domain"], "ok": result["ok"], "exported_urls": result["exported_urls"], "done": len(results), "total": len(targets)})
                    if next_index < len(targets):
                        domain, output_name = targets[next_index]
                        futures[pool.submit(export_domain_urls, domain, output_name, args)] = domain
                        next_index += 1

    if processed_f is not None:
        processed_f.close()

    summary = {
        "crawl_id": args.crawl_id,
        "index_endpoint": index_endpoint(args.crawl_id),
        "domains_requested": len(targets) + skipped_processed,
        "domains_skipped_already_processed": skipped_processed,
        "domains_processed": len(results),
        "ok_domains": totals.get("ok_domains", 0),
        "failed_domains": totals.get("failed_domains", 0),
        "capped_domains": totals.get("capped_domains", 0),
        "exported_urls": totals.get("exported_urls", 0),
        "dedup": {
            "skipped_duplicate_digest": totals.get("skipped_duplicate_digest", 0),
            "skipped_duplicate_url": totals.get("skipped_duplicate_url", 0),
            "skipped_non_html": totals.get("skipped_non_html", 0),
        },
        "processed_domains_file": args.processed_domains_file or "",
        "output_dir": args.output_dir,
        "elapsed_seconds": round(time.time() - started, 2),
        "counters": dict(totals),
    }
    Path(args.summary_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit_progress({"stage": "seeder_done", "ok_domains": summary["ok_domains"], "failed_domains": summary["failed_domains"], "exported_urls": summary["exported_urls"]})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
