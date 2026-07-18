#!/usr/bin/env python3
"""
Export every English-content registered domain from a full Common Crawl
columnar index (all ~300 parquet files of one crawl) with DuckDB.

Scans one index file at a time and checkpoints progress, so an
interrupted run resumes where it stopped instead of restarting the
multi-hour scan. Per-file distinct domains are appended to a shard file;
the final merge produces one globally deduplicated domain list with
per-domain English page counts.

Example:
  python3 src/cc_english_domain_exporter.py \
    --crawl-id CC-MAIN-2025-08 \
    --work-dir data/cc_english_domains \
    --output-tsv cc_english_domains.tsv
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
        description="Export all English-content registered domains from a CC columnar index"
    )
    parser.add_argument("--crawl-id", default="CC-MAIN-2025-08")
    parser.add_argument("--work-dir", default="data/cc_english_domains")
    parser.add_argument("--output-tsv", default="cc_english_domains.tsv")
    parser.add_argument(
        "--max-index-files",
        type=int,
        default=0,
        help="0 = scan every index file of the crawl",
    )
    parser.add_argument(
        "--english-only-strict",
        action="store_true",
        help="Require content_languages == 'eng' exactly instead of containing 'eng'",
    )
    parser.add_argument("--http-threads", type=int, default=8)
    parser.add_argument("--max-retries", type=int, default=8)
    return parser.parse_args()


def list_index_files(crawl_id, timeout=60):
    paths_url = f"{DATA_BASE_URL}crawl-data/{crawl_id}/cc-index-table.paths.gz"
    with urlopen(paths_url, timeout=timeout) as response:
        paths = gzip.decompress(response.read()).decode("utf-8").splitlines()
    warc_paths = [p for p in paths if "/subset=warc/" in p and p.endswith(".parquet")]
    return [DATA_BASE_URL + p for p in sorted(warc_paths)]


def scan_file(con, url, language_filter, max_retries):
    query = f"""
        SELECT url_host_registered_domain AS domain, COUNT(*) AS pages
        FROM read_parquet($url)
        WHERE fetch_status = 200
          AND content_mime_detected = 'text/html'
          AND {language_filter}
          AND url_host_registered_domain IS NOT NULL
        GROUP BY 1
    """
    for attempt in range(1, max_retries + 1):
        try:
            return con.execute(query, {"url": url}).fetchall()
        except (duckdb.HTTPException, duckdb.IOException) as error:
            if attempt == max_retries:
                raise
            delay = min(120, 2**attempt)
            emit_progress(
                {
                    "stage": "retry",
                    "file": url.rsplit("/", 1)[-1],
                    "attempt": attempt,
                    "sleep_seconds": delay,
                    "error": str(error).splitlines()[0],
                }
            )
            time.sleep(delay)


def merge_shards(shards_dir, output_tsv):
    con = duckdb.connect()
    con.execute(
        """
        COPY (
            SELECT domain, SUM(pages) AS english_pages
            FROM read_csv_auto($glob, delim='\t', header=false,
                               names=['domain', 'pages'])
            GROUP BY 1
            ORDER BY english_pages DESC
        ) TO $out (DELIMITER '\t', HEADER false)
        """,
        {"glob": str(Path(shards_dir) / "*.tsv"), "out": output_tsv},
    )
    return con.execute(
        "SELECT COUNT(*) FROM read_csv_auto($f, delim='\t', header=false)",
        {"f": output_tsv},
    ).fetchone()[0]


def main():
    args = parse_args()
    started = time.time()
    work_dir = Path(args.work_dir)
    shards_dir = work_dir / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    done_path = work_dir / "processed_files.txt"
    done = set(done_path.read_text().splitlines()) if done_path.exists() else set()

    files = list_index_files(args.crawl_id)
    if args.max_index_files > 0:
        files = files[: args.max_index_files]
    emit_progress(
        {"stage": "start", "index_files": len(files), "already_done": len(done)}
    )

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET threads={max(1, args.http_threads)};")
    language_filter = (
        "content_languages = 'eng'"
        if args.english_only_strict
        else "content_languages LIKE '%eng%'"
    )

    for position, url in enumerate(files, 1):
        name = url.rsplit("/", 1)[-1]
        if name in done:
            continue
        file_started = time.time()
        rows = scan_file(con, url, language_filter, args.max_retries)
        shard_path = shards_dir / f"{name}.tsv"
        with shard_path.open("w", encoding="utf-8") as handle:
            for domain, pages in rows:
                handle.write(f"{domain}\t{pages}\n")
        with done_path.open("a", encoding="utf-8") as handle:
            handle.write(name + "\n")
        emit_progress(
            {
                "stage": "file_done",
                "position": position,
                "total": len(files),
                "file": name,
                "domains_in_file": len(rows),
                "file_seconds": round(time.time() - file_started, 1),
                "elapsed_seconds": round(time.time() - started, 1),
            }
        )

    total_domains = merge_shards(shards_dir, args.output_tsv)
    summary = {
        "crawl_id": args.crawl_id,
        "index_files_scanned": len(files),
        "unique_english_domains": total_domains,
        "english_only_strict": args.english_only_strict,
        "elapsed_seconds": round(time.time() - started, 1),
        "output_tsv": args.output_tsv,
    }
    (work_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
