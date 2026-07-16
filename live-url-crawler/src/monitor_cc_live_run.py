#!/usr/bin/env python3
import argparse
import json
import shutil
import time
from pathlib import Path


def count_lines(path):
    path = Path(path)
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def numeric_value(row, *names):
    for name in names:
        value = row.get(name)
        if isinstance(value, (int, float)):
            return value
    return 0


def merge_pages(run_root, partial_jsonl, state_json):
    partial_path = Path(partial_jsonl)
    state_path = Path(state_json)
    state = load_json(state_path) or {}
    files = state.get("files") or {}
    pages_paths = sorted(Path(run_root).glob("*/pages.jsonl"))
    initialized = False
    if partial_path.exists() and not files:
        for pages_path in pages_paths:
            try:
                files[str(pages_path)] = pages_path.stat().st_size
            except FileNotFoundError:
                continue
        initialized = True
    appended_bytes = 0
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    with partial_path.open("ab") as out:
        for pages_path in pages_paths:
            try:
                size = pages_path.stat().st_size
            except FileNotFoundError:
                continue
            key = str(pages_path)
            offset = int(files.get(key, 0))
            if size < offset:
                offset = 0
            if size <= offset:
                files[key] = size
                continue
            with pages_path.open("rb") as f:
                f.seek(offset)
                shutil.copyfileobj(f, out)
            appended_bytes += size - offset
            files[key] = size
    state_path.write_text(json.dumps({"files": files, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"partial_appended_bytes": appended_bytes, "partial_merge_files": len(pages_paths), "partial_merge_initialized": initialized}


def collect_snapshot(args, started_at, previous_records, previous_ts, merge_info=None):
    now = time.time()
    run_root = Path(args.run_root)
    site_dirs = [p for p in run_root.iterdir() if p.is_dir()] if run_root.exists() else []
    summaries = []
    progress_rows = []
    pages_records_exact = 0
    manifest_records_exact = 0
    for site_dir in site_dirs:
        if args.count_jsonl_lines:
            pages_records_exact += count_lines(site_dir / "pages.jsonl")
            manifest_records_exact += count_lines(site_dir / "manifest.jsonl")
        summary = load_json(site_dir / "summary.json")
        if summary:
            summaries.append(summary)
        progress = load_json(site_dir / "progress.json")
        if progress:
            progress_rows.append(progress)
    complete_sites = sum(1 for row in summaries if row.get("site_crawl_complete") and row.get("stopped_reason") == "frontier_exhausted")
    capped_sites = sum(1 for row in summaries if row.get("stopped_reason") in {"max_pages_reached", "max_discovered_reached"})
    finished_sites = len(summaries)
    running_sites = max(0, len(site_dirs) - finished_sites)
    summary_pages = sum(numeric_value(row, "pages_written", "unique_extracted_text_records", "successful_pages", "fetched_pages") for row in summaries)
    running_pages = sum(numeric_value(row, "unique_extracted_text_records", "ok_manifest_records", "fetched_pages") for row in progress_rows)
    summary_manifest = sum(numeric_value(row, "ok_manifest_records", "successful_pages", "fetched_pages") for row in summaries)
    running_manifest = sum(numeric_value(row, "ok_manifest_records", "fetched_pages") for row in progress_rows)
    pages_records = pages_records_exact if args.count_jsonl_lines else summary_pages + running_pages
    manifest_records = manifest_records_exact if args.count_jsonl_lines else summary_manifest + running_manifest
    elapsed = max(0.001, now - started_at)
    interval_seconds = max(0.001, now - previous_ts)
    if previous_records is None:
        delta_records = 0
        records_per_minute = 0.0
    else:
        delta_records = pages_records - previous_records
        records_per_minute = round(delta_records * 60.0 / interval_seconds, 2)
    partial_path = Path(args.partial_jsonl)
    try:
        partial_size_bytes = partial_path.stat().st_size
    except FileNotFoundError:
        partial_size_bytes = 0
    snapshot = {
        "timestamp_epoch": round(now, 3),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(elapsed, 2),
        "run_root": str(run_root),
        "site_dirs": len(site_dirs),
        "finished_sites": finished_sites,
        "running_sites": running_sites,
        "full_site_verified_sites": complete_sites,
        "capped_or_incomplete_sites": capped_sites,
        "pages_jsonl_records": pages_records,
        "manifest_jsonl_records": manifest_records,
        "delta_records": delta_records,
        "interval_seconds": round(interval_seconds, 2),
        "records_per_minute": records_per_minute,
        "target_records_per_minute": args.target_records_per_minute,
        "target_rate_ratio": round(records_per_minute / args.target_records_per_minute, 4) if args.target_records_per_minute else 0.0,
        "records_per_second_avg": round(pages_records / elapsed, 4),
        "scheduled_pages_sum": sum(row.get("scheduled_pages", 0) for row in progress_rows),
        "fetched_pages_sum": sum(row.get("fetched_pages", 0) for row in progress_rows),
        "ok_manifest_records_sum": sum(row.get("ok_manifest_records", 0) for row in progress_rows),
        "unique_extracted_text_records_sum": sum(row.get("unique_extracted_text_records", 0) for row in progress_rows),
        "remaining_frontier_sum": sum(row.get("remaining_frontier", 0) for row in progress_rows),
        "counts_mode": "jsonl_line_count" if args.count_jsonl_lines else "progress_summary",
        "partial_size_bytes": partial_size_bytes,
        "partial_jsonl": str(partial_path.resolve()),
        "stats_jsonl": str(Path(args.stats_jsonl).resolve()),
        "latest_json": str(Path(args.latest_json).resolve()),
    }
    if merge_info:
        snapshot.update(merge_info)
    return snapshot


def parse_args():
    parser = argparse.ArgumentParser(description="Minute-level monitor for Common Crawl seeded live crawls")
    parser.add_argument("--run-root", default="data/runs/cc_live_sites_full")
    parser.add_argument("--partial-jsonl", default="cc_live_pages_partial.jsonl")
    parser.add_argument("--stats-jsonl", default="cc_live_run_stats.jsonl")
    parser.add_argument("--latest-json", default="cc_live_run_latest.json")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--merge-partial", action="store_true")
    parser.add_argument("--partial-state-json")
    parser.add_argument("--count-jsonl-lines", action="store_true")
    parser.add_argument("--target-records-per-minute", type=float, default=50000.0)
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.partial_state_json:
        args.partial_state_json = str(Path(str(args.partial_jsonl) + ".state.json"))
    started_at = time.time()
    previous_records = None
    previous_ts = started_at
    while True:
        merge_info = {}
        if args.merge_partial:
            merge_info = merge_pages(args.run_root, Path(args.partial_jsonl), args.partial_state_json)
        snapshot = collect_snapshot(args, started_at, previous_records, previous_ts, merge_info)
        previous_records = snapshot["pages_jsonl_records"]
        previous_ts = time.time()
        with Path(args.stats_jsonl).open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        Path(args.latest_json).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(snapshot, ensure_ascii=False, indent=2), flush=True)
        if args.once:
            break
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    main()
