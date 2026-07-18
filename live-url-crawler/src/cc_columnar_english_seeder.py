#!/usr/bin/env python3
"""
Bulk-export English content URLs from the Common Crawl columnar index
(parquet) with DuckDB, producing seed URL lists for the live crawler.

Compared to cc_index_url_seeder.py (CDX API, per-domain queries), this
tool scans the columnar index directly, so it can pull millions of
verified URLs in one pass without knowing domains up front. Every
exported URL is already confirmed by Common Crawl to be:
  - fetch_status = 200
  - content_mime_detected = text/html
  - content_languages contains 'eng'

Pipeline position:
  cc_columnar_english_seeder.py  ->  seeds TSV / per-domain URL JSONL
        (bulk English URLs)          -> pipeline_domain_crawler.py / multiproc_async_crawler.py

Examples:
  # Quick sample: 50k English URLs from 2 index files
  python3 src/cc_columnar_english_seeder.py \
    --crawl-id CC-MAIN-2025-08 \
    --max-index-files 2 \
    --max-urls 50000 \
    --output-urls-jsonl cc_english_urls.jsonl \
    --output-seeds-tsv cc_english_seeds.tsv

  # Larger export with per-domain URL caps and content-length floor
  python3 src/cc_columnar_english_seeder.py \
    --crawl-id CC-MAIN-2025-08 \
    --max-index-files 20 \
    --max-urls 2000000 \
    --max-urls-per-domain 200 \
    --min-content-length 5000 \
    --english-only-strict \
    --output-urls-jsonl cc_english_urls.jsonl \
    --output-seeds-tsv cc_english_seeds.tsv
"""
import argparse
import gzip
import json
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import duckdb

DATA_BASE_URL = "https://data.commoncrawl.org/"


def emit_progress(row):
    row = dict(row)
    row["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(json.dumps(row, ensure_ascii=False), file=sys.stderr, flush=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Export English content URLs from the Common Crawl columnar index"
    )
    parser.add_argument("--crawl-id", default="CC-MAIN-2025-08")
    parser.add_argument(
        "--max-index-files",
        type=int,
        default=2,
        help="Number of index parquet files to scan (there are ~300 per crawl; "
        "each holds a similar URL mix, so a few files already yield millions of rows)",
    )
    parser.add_argument("--max-urls", type=int, default=50000)
    parser.add_argument(
        "--max-urls-per-domain",
        type=int,
        default=100,
        help="Cap URLs per registered domain to keep the seed list diverse",
    )
    parser.add_argument(
        "--min-content-length",
        type=int,
        default=2000,
        help="Skip pages whose stored WARC record is smaller than this many bytes "
        "(filters near-empty pages)",
    )
    parser.add_argument(
        "--english-only-strict",
        action="store_true",
        help="Require content_languages == 'eng' exactly (monolingual English) "
        "instead of merely containing 'eng'",
    )
    parser.add_argument("--http-threads", type=int, default=8)
    parser.add_argument(
        "--max-retries",
        type=int,
        default=6,
        help="Retries for transient data.commoncrawl.org 503 throttling",
    )
    parser.add_argument("--output-urls-jsonl", default="cc_english_urls.jsonl")
    parser.add_argument("--output-seeds-tsv", default="cc_english_seeds.tsv")
    parser.add_argument("--summary-json", default="cc_english_seeder_summary.json")
    return parser.parse_args()


def connect(http_threads):
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET threads={max(1, http_threads)};")
    return con


def list_index_files(crawl_id, max_index_files, timeout=60):
    paths_url = f"{DATA_BASE_URL}crawl-data/{crawl_id}/cc-index-table.paths.gz"
    with urlopen(paths_url, timeout=timeout) as response:
        paths = gzip.decompress(response.read()).decode("utf-8").splitlines()
    warc_paths = [p for p in paths if "/subset=warc/" in p and p.endswith(".parquet")]
    return [DATA_BASE_URL + p for p in sorted(warc_paths)[:max_index_files]]


def export_urls(con, files, args):
    language_filter = (
        "content_languages = 'eng'"
        if args.english_only_strict
        else "content_languages LIKE '%eng%'"
    )
    query = f"""
        WITH hits AS (
            SELECT
                url,
                url_host_name AS host,
                url_host_registered_domain AS registered_domain,
                warc_record_length AS record_length,
                content_digest,
                ROW_NUMBER() OVER (PARTITION BY content_digest ORDER BY url) AS digest_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY url_host_registered_domain ORDER BY warc_record_length DESC
                ) AS domain_rank
            FROM read_parquet($files)
            WHERE fetch_status = 200
              AND content_mime_detected = 'text/html'
              AND {language_filter}
              AND warc_record_length >= $min_length
              AND url_host_registered_domain IS NOT NULL
        )
        SELECT url, host, registered_domain, record_length
        FROM hits
        WHERE digest_rank = 1 AND domain_rank <= $per_domain
        LIMIT $max_urls
    """
    params = {
        "files": files,
        "min_length": args.min_content_length,
        "per_domain": args.max_urls_per_domain,
        "max_urls": args.max_urls,
    }
    attempts = max(1, args.max_retries)
    for attempt in range(1, attempts + 1):
        try:
            return con.execute(query, params).fetchall()
        except duckdb.HTTPException as error:
            if attempt == attempts:
                raise
            delay = min(60, 2**attempt)
            emit_progress(
                {
                    "stage": "retry",
                    "attempt": attempt,
                    "sleep_seconds": delay,
                    "error": str(error).splitlines()[0],
                }
            )
            time.sleep(delay)


def write_outputs(rows, args):
    domains = {}
    urls_path = Path(args.output_urls_jsonl)
    urls_path.parent.mkdir(parents=True, exist_ok=True)
    with urls_path.open("w", encoding="utf-8") as handle:
        for url, host, registered_domain, record_length in rows:
            handle.write(
                json.dumps(
                    {
                        "url": url,
                        "host": host,
                        "registered_domain": registered_domain,
                        "record_length": record_length,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            if registered_domain not in domains:
                domains[registered_domain] = url

    seeds_path = Path(args.output_seeds_tsv)
    seeds_path.parent.mkdir(parents=True, exist_ok=True)
    with seeds_path.open("w", encoding="utf-8") as handle:
        handle.write("# seed_url\tscope_domain_or_host\toutput_name\n")
        for registered_domain, url in sorted(domains.items()):
            handle.write(f"{url}\t{registered_domain}\t{registered_domain}\n")
    return len(domains)


def main():
    args = parse_args()
    started = time.time()
    con = connect(args.http_threads)

    emit_progress({"stage": "list_index_files", "crawl_id": args.crawl_id})
    files = list_index_files(args.crawl_id, args.max_index_files)
    if not files:
        emit_progress({"stage": "error", "message": "no index files found"})
        return 1
    emit_progress({"stage": "query_start", "index_files": len(files)})

    rows = export_urls(con, files, args)
    domain_count = write_outputs(rows, args)

    summary = {
        "crawl_id": args.crawl_id,
        "index_files_scanned": len(files),
        "urls_exported": len(rows),
        "unique_domains": domain_count,
        "min_content_length": args.min_content_length,
        "max_urls_per_domain": args.max_urls_per_domain,
        "english_only_strict": args.english_only_strict,
        "elapsed_seconds": round(time.time() - started, 1),
        "outputs": {
            "urls_jsonl": args.output_urls_jsonl,
            "seeds_tsv": args.output_seeds_tsv,
        },
    }
    Path(args.summary_json).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
