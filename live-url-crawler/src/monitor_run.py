#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path


def load_json(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def tail_lines(path, count):
    p = Path(path)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-count:]


def print_once(run_dir, tail):
    run_dir = Path(run_dir)
    progress = load_json(run_dir / "progress.json")
    summary = load_json(run_dir / "summary.json")
    data = progress or summary
    if not data:
        print(f"waiting_for_progress run_dir={run_dir}", flush=True)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)
    if tail:
        print("--- crawl.log tail ---", flush=True)
        for line in tail_lines(run_dir / "crawl.log", tail):
            print(line, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Monitor a running domain crawler output directory")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--tail", type=int, default=10)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()

    while True:
        print_once(args.run_dir, args.tail)
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
