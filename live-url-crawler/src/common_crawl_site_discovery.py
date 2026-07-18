#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DATA_BASE_URL = "https://data.commoncrawl.org/"
USER_AGENT = "common-crawl-site-discovery/0.1"
ADULT_OR_SPAM_TERMS = {
    "sex", "porn", "xxx", "casino", "bet", "gambling", "viagra", "loan", "payday",
}
ENGLISH_FRIENDLY_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "int", "info", "biz", "name", "pro",
    "io", "co", "ai", "app", "dev", "tech", "cloud", "site", "online", "blog", "news",
    "media", "digital", "software", "systems", "company", "services", "solutions",
}
ENGLISH_COUNTRY_TLDS = {"us", "uk", "ca", "au", "nz", "ie"}
DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9-]+$")
MULTI_LABEL_PUBLIC_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "sch.uk", "net.uk", "me.uk", "ltd.uk", "plc.uk",
    "co.nz", "org.nz", "net.nz", "govt.nz", "ac.nz",
    "com.au", "net.au", "org.au", "gov.au", "edu.au", "id.au",
    "co.ie", "org.ie",
    "co.il", "gov.il", "co.in", "gov.in",
    "co.za", "org.za", "gov.za",
    "com.sg", "com.hk", "com.my",
}


def registrable_domain(host):
    """Approximate eTLD+1: collapse subdomains (including www) onto one
    registrable domain so that www.example.com and example.com, or
    blog.example.co.uk and example.co.uk, dedupe to the same site."""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in MULTI_LABEL_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


def write_jsonl_row(handle, row):
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def emit_progress(args, row):
    row = dict(row)
    row["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    row["timestamp_epoch"] = round(time.time(), 3)
    line = json.dumps(row, ensure_ascii=False)
    print(line, file=sys.stderr, flush=True)
    if args.progress_json:
        Path(args.progress_json).write_text(line + "\n", encoding="utf-8")


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "site"


def path_to_url(path_or_url):
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return DATA_BASE_URL + path_or_url.lstrip("/")


def open_remote(path_or_url, timeout):
    request = Request(path_to_url(path_or_url), headers={"User-Agent": USER_AGENT})
    return urlopen(request, timeout=timeout)


def read_wet_paths(args):
    if args.wet_paths_file:
        paths = []
        emit_progress(args, {"stage": "read_wet_paths_file", "wet_paths_file": args.wet_paths_file})
        with Path(args.wet_paths_file).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    paths.append(line)
        emit_progress(args, {"stage": "wet_paths_loaded", "wet_paths": len(paths)})
        return paths
    url = f"{DATA_BASE_URL}crawl-data/{args.crawl_id}/wet.paths.gz"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    emit_progress(args, {"stage": "download_wet_paths_start", "url": url})
    with urlopen(request, timeout=args.timeout) as response:
        data = response.read()
    emit_progress(args, {"stage": "download_wet_paths_done", "bytes": len(data)})
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        paths = [line.decode("utf-8").strip() for line in gz if line.strip()]
    emit_progress(args, {"stage": "wet_paths_loaded", "wet_paths": len(paths)})
    return paths


def read_text_line(stream):
    line = stream.readline()
    if not line:
        return None
    return line.decode("utf-8", errors="replace").rstrip("\r\n")


def parse_warc_headers(gz_stream):
    while True:
        line = read_text_line(gz_stream)
        if line is None:
            return
        if not line.startswith("WARC/"):
            continue
        headers = {}
        while True:
            header_line = read_text_line(gz_stream)
            if header_line is None:
                return
            if header_line == "":
                break
            key, sep, value = header_line.partition(":")
            if sep:
                headers[key.strip()] = value.strip()
        try:
            length = int(headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > 0:
            gz_stream.read(length)
        gz_stream.readline()
        yield headers


def is_english_domain_candidate(host):
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        return False
    labels = host.split(".")
    if any(not label or label.startswith("-") or label.endswith("-") for label in labels):
        return False
    if any(label.startswith("xn--") for label in labels):
        return False
    if any(not DOMAIN_LABEL_RE.match(label) for label in labels):
        return False
    tld = labels[-1]
    if tld in ENGLISH_FRIENDLY_TLDS or tld in ENGLISH_COUNTRY_TLDS:
        return True
    return False


def site_from_target_url(target_url, english_domain_only=False):
    parsed = urlparse(target_url)
    host = (parsed.hostname or "").lower().strip(".")
    if not host or "." not in host:
        return None
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return None
    first_label = host.split(".", 1)[0]
    if not re.search(r"[a-z]", first_label):
        return None
    host_labels = re.split(r"[.-]", host)
    if any(term in host_labels for term in ADULT_OR_SPAM_TERMS):
        return None
    if english_domain_only and not is_english_domain_candidate(host):
        return None
    scheme = "https" if parsed.scheme == "https" else "http"
    domain = registrable_domain(host)
    canonical_host = host[4:] if host.startswith("www.") and host != "www." + domain else host
    return {
        "seed_url": f"{scheme}://{canonical_host}/",
        "scope": domain,
        "host": host,
        "registrable_domain": domain,
        "sample_url": target_url,
    }


def discover_sites_from_wet_once(path, args):
    rows = []
    counters = Counter()
    with open_remote(path, args.timeout) as response:
        with gzip.GzipFile(fileobj=response) as gz:
            for headers in parse_warc_headers(gz):
                counters["warc_records"] += 1
                target_url = headers.get("WARC-Target-URI", "")
                if not target_url:
                    counters["missing_target_url"] += 1
                    continue
                site = site_from_target_url(target_url, args.english_domain_only)
                if not site:
                    counters["invalid_site"] += 1
                    continue
                site["source"] = "common_crawl_wet_header"
                site["source_wet_path"] = path
                site["source_wet_url"] = path_to_url(path)
                site["record_id"] = headers.get("WARC-Record-ID", "")
                site["discovered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                rows.append(site)
                counters["candidate_sites"] += 1
                if args.max_sites_per_wet and len(rows) >= args.max_sites_per_wet:
                    break
                if args.max_records_per_wet and counters["warc_records"] >= args.max_records_per_wet:
                    break
    counters["ok_wet_files"] += 1
    return rows, counters


def discover_sites_from_wet(path, args, max_retries=3):
    started = time.time()
    last_error = None
    for attempt in range(max_retries):
        try:
            rows, counters = discover_sites_from_wet_once(path, args)
            counters["elapsed_seconds"] = round(time.time() - started, 2)
            counters["attempts"] = attempt + 1
            return path, rows, counters
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(min(30, 5 * (attempt + 1)))
    counters = Counter()
    counters["failed_wet_files"] = 1
    counters["elapsed_seconds"] = round(time.time() - started, 2)
    counters["attempts"] = max_retries
    rows = [{"error": repr(last_error), "source_wet_path": path, "source_wet_url": path_to_url(path)}]
    return path, rows, counters


def select_wet_paths(paths, max_wet_files, spread):
    if not max_wet_files or len(paths) <= max_wet_files:
        return paths
    if not spread:
        return paths[:max_wet_files]
    step = len(paths) / max_wet_files
    return [paths[min(len(paths) - 1, int(i * step))] for i in range(max_wet_files)]


def parse_args():
    parser = argparse.ArgumentParser(description="Discover live crawl seed sites from Common Crawl WET headers")
    parser.add_argument("--crawl-id", default="CC-MAIN-2025-08")
    parser.add_argument("--wet-paths-file")
    parser.add_argument("--max-wet-files", type=int, default=20)
    parser.add_argument("--spread-wet-paths", action="store_true")
    parser.add_argument("--max-sites", type=int, default=1000)
    parser.add_argument("--max-sites-per-wet", type=int, default=10000)
    parser.add_argument("--max-records-per-wet", type=int, default=200000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--english-domain-only", action="store_true")
    parser.add_argument("--output-sites-jsonl", default="cc_sites.jsonl")
    parser.add_argument("--output-seeds-tsv", default="cc_seed_sites.tsv")
    parser.add_argument("--output-manifest-jsonl", default="cc_site_discovery_manifest.jsonl")
    parser.add_argument("--summary-json", default="cc_site_discovery_summary.json")
    parser.add_argument("--progress-json")
    parser.add_argument(
        "--seen-domains-file",
        help="Persisted registrable-domain registry shared across runs. "
        "Domains already present are skipped as duplicates, and newly "
        "accepted domains are appended so later runs (even with a "
        "different --crawl-id) do not rediscover the same sites.",
    )
    parser.add_argument(
        "--processed-wets-file",
        help="Persisted registry of WET paths that were fully consumed by "
        "previous runs. Listed paths are skipped so re-runs and crash "
        "restarts never re-download or re-parse the same WET file. A path "
        "is only registered when every row in it was consumed (a file cut "
        "short by --max-sites or a download failure stays unregistered and "
        "is retried next run).",
    )
    return parser.parse_args()


def load_seen_domains(path):
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    with p.open("r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def load_processed_wets(path):
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    with p.open("r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def main():
    args = parse_args()
    started = time.time()
    emit_progress(args, {"stage": "discovery_start", "crawl_id": args.crawl_id, "max_wet_files": args.max_wet_files, "max_sites": args.max_sites, "english_domain_only": args.english_domain_only, "spread_wet_paths": args.spread_wet_paths})
    all_wet_paths = read_wet_paths(args)
    processed_wets = load_processed_wets(args.processed_wets_file)
    skipped_processed_wets = 0
    if processed_wets:
        before = len(all_wet_paths)
        all_wet_paths = [p for p in all_wet_paths if p not in processed_wets]
        skipped_processed_wets = before - len(all_wet_paths)
        emit_progress(args, {"stage": "processed_wets_filtered", "already_processed": skipped_processed_wets, "remaining_wet_paths": len(all_wet_paths)})
    wet_paths = select_wet_paths(all_wet_paths, args.max_wet_files, args.spread_wet_paths)
    emit_progress(args, {"stage": "wet_paths_selected", "wet_paths_available": len(all_wet_paths), "wet_paths_considered": len(wet_paths), "spread_wet_paths": args.spread_wet_paths})
    processed_f = Path(args.processed_wets_file).open("a", encoding="utf-8") if args.processed_wets_file else None
    seen_domains = load_seen_domains(args.seen_domains_file)
    preloaded_domains = len(seen_domains)
    newly_seen_domains = []
    tld_counts = Counter()
    counters = Counter()
    next_index = 0

    sites_path = Path(args.output_sites_jsonl)
    seeds_path = Path(args.output_seeds_tsv)
    manifest_path = Path(args.output_manifest_jsonl)

    with sites_path.open("w", encoding="utf-8") as sites_f, seeds_path.open("w", encoding="utf-8") as seeds_f, manifest_path.open("w", encoding="utf-8") as manifest_f:
        seeds_f.write("# seed_url\tscope_domain_or_host\toutput_name\n")
        futures = {}
        accepted = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            while next_index < len(wet_paths) and len(futures) < args.workers:
                path = wet_paths[next_index]
                futures[pool.submit(discover_sites_from_wet, path, args)] = path
                next_index += 1
                emit_progress(args, {"stage": "wet_path_submitted", "submitted_wet_paths": next_index, "active_workers": len(futures), "unique_sites": accepted})
            while futures and accepted < args.max_sites:
                done, _ = wait(set(futures), timeout=0.2, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    futures.pop(future)
                    path, rows, file_counters = future.result()
                    counters.update(file_counters)
                    emit_progress(args, {"stage": "wet_path_done", "wet_path": path, "submitted_wet_paths": next_index, "active_workers": len(futures), "unique_sites": accepted, "file_counters": dict(file_counters)})
                    errors = [row for row in rows if row.get("error")]
                    write_jsonl_row(manifest_f, {"wet_path": path, "counters": dict(file_counters), "errors": errors[:5]})
                    consumed_all_rows = True
                    for row_index, row in enumerate(rows):
                        if row.get("error"):
                            continue
                        domain = row["registrable_domain"]
                        if domain in seen_domains:
                            counters["duplicate_hosts"] += 1
                            continue
                        seen_domains.add(domain)
                        newly_seen_domains.append(domain)
                        tld_counts[domain.rsplit(".", 1)[-1]] += 1
                        output_name = safe_name(domain)
                        row["output_name"] = output_name
                        write_jsonl_row(sites_f, row)
                        seeds_f.write(f"{row['seed_url']}\t{row['scope']}\t{output_name}\n")
                        counters["unique_sites"] += 1
                        accepted += 1
                        if accepted >= args.max_sites:
                            consumed_all_rows = row_index == len(rows) - 1
                            break
                    if processed_f is not None and consumed_all_rows and not file_counters.get("failed_wet_files"):
                        processed_f.write(path + "\n")
                        processed_f.flush()
                        counters["registered_processed_wets"] += 1
                    sites_f.flush()
                    seeds_f.flush()
                    manifest_f.flush()
                    if next_index < len(wet_paths) and accepted < args.max_sites:
                        path = wet_paths[next_index]
                        futures[pool.submit(discover_sites_from_wet, path, args)] = path
                        next_index += 1

    if processed_f is not None:
        processed_f.close()

    if args.seen_domains_file and newly_seen_domains:
        with Path(args.seen_domains_file).open("a", encoding="utf-8") as f:
            for domain in newly_seen_domains:
                f.write(domain + "\n")

    ok_wet_files = counters.get("ok_wet_files", 0)
    unique_sites = len(newly_seen_domains)
    summary = {
        "crawl_id": args.crawl_id,
        "wet_paths_considered": len(wet_paths),
        "unique_sites": unique_sites,
        "elapsed_seconds": round(time.time() - started, 2),
        "english_domain_only": args.english_domain_only,
        "spread_wet_paths": args.spread_wet_paths,
        "dedup_unit": "registrable_domain",
        "seen_domains_file": args.seen_domains_file or "",
        "preloaded_domains_from_registry": preloaded_domains,
        "total_domains_in_registry_after_run": len(seen_domains),
        "processed_wets_file": args.processed_wets_file or "",
        "wet_paths_skipped_already_processed": skipped_processed_wets,
        "wet_paths_registered_processed_this_run": counters.get("registered_processed_wets", 0),
        "unique_sites_per_ok_wet_file": round(unique_sites / ok_wet_files, 2) if ok_wet_files else 0,
        "candidate_sites_per_ok_wet_file": round(counters.get("candidate_sites", 0) / ok_wet_files, 2) if ok_wet_files else 0,
        "accepted_tld_counts_top50": tld_counts.most_common(50),
        "counters": dict(counters),
        "files": {
            "sites_jsonl": str(sites_path),
            "seed_sites_tsv": str(seeds_path),
            "manifest_jsonl": str(manifest_path),
        },
    }
    Path(args.summary_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit_progress(args, {"stage": "discovery_done", "unique_sites": unique_sites, "ok_wet_files": ok_wet_files, "failed_wet_files": counters.get("failed_wet_files", 0)})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
