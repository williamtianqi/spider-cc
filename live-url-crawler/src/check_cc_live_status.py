#!/usr/bin/env python3
import argparse
import json
import subprocess
import time
from pathlib import Path


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def stat_file(path):
    path = Path(path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    now = time.time()
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": stat.st_size,
        "size_mb": round(stat.st_size / 1024 / 1024, 3),
        "mtime_epoch": round(stat.st_mtime, 3),
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "age_seconds": round(now - stat.st_mtime, 1),
    }


def process_counts(run_root, latest_json):
    try:
        output = subprocess.check_output(["ps", "-ax", "-o", "command="], text=True, errors="replace")
    except Exception:
        return {}
    lines = output.splitlines()
    return {
        "pipeline_domain_crawler": sum(1 for line in lines if "src/pipeline_domain_crawler.py" in line and run_root in line),
        "multi_seed_runner": sum(1 for line in lines if "run_pipeline_multi_seed_turbo.sh" in line and run_root in line),
        "monitor": sum(1 for line in lines if "monitor_cc_live_run.py" in line and latest_json in line),
        "common_crawl_discovery": sum(1 for line in lines if "common_crawl_site_discovery.py" in line),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Lightweight status check for Common Crawl seeded live crawls")
    parser.add_argument("--run-root", default="data/runs/cc_live_sites_100x")
    parser.add_argument("--latest-json", default="cc_live_run_latest_100x.json")
    parser.add_argument("--partial-jsonl", default="cc_live_pages_100x_partial.jsonl")
    parser.add_argument("--stats-jsonl", default="cc_live_run_stats_100x.jsonl")
    return parser.parse_args()


def main():
    args = parse_args()
    latest = load_json(args.latest_json) or {}
    output = {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "latest": latest,
        "files": {
            "latest_json": stat_file(args.latest_json),
            "partial_jsonl": stat_file(args.partial_jsonl),
            "stats_jsonl": stat_file(args.stats_jsonl),
        },
        "process_counts": process_counts(args.run_root, args.latest_json),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
