#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def count_lines(path):
    if not path or not Path(path).exists():
        return 0
    with Path(path).open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_args():
    parser = argparse.ArgumentParser(description="Verify whether per-site crawls completed by exhausting frontier")
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--final-jsonl", required=True)
    parser.add_argument("--output-json", default="cc_full_site_verification.json")
    parser.add_argument("--output-jsonl", default="cc_full_site_verification_sites.jsonl")
    return parser.parse_args()


def main():
    args = parse_args()
    run_root = Path(args.run_root)
    rows = []
    for summary_path in sorted(run_root.glob("*/summary.json")):
        summary = load_json(summary_path)
        pages_path = summary.get("files", {}).get("pages", "")
        manifest_path = summary.get("files", {}).get("manifest", "")
        frontier_path = summary.get("files", {}).get("frontier_remaining", "")
        row = {
            "seed_url": summary.get("seed_url", ""),
            "summary_path": str(summary_path),
            "pages_path": pages_path,
            "manifest_path": manifest_path,
            "frontier_path": frontier_path,
            "site_crawl_complete": bool(summary.get("site_crawl_complete")),
            "stopped_reason": summary.get("stopped_reason", ""),
            "remaining_frontier": summary.get("remaining_frontier", 0),
            "scheduled_pages": summary.get("scheduled_pages", 0),
            "fetched_pages": summary.get("fetched_pages", 0),
            "ok_manifest_records": summary.get("ok_manifest_records", 0),
            "unique_text_pages": summary.get("unique_text_pages", 0),
            "pages_jsonl_records": count_lines(pages_path),
            "manifest_jsonl_records": count_lines(manifest_path),
            "frontier_jsonl_records": count_lines(frontier_path),
        }
        row["full_site_verified"] = (
            row["site_crawl_complete"]
            and row["stopped_reason"] == "frontier_exhausted"
            and row["remaining_frontier"] == 0
            and row["frontier_jsonl_records"] == 0
        )
        rows.append(row)

    final_records = count_lines(args.final_jsonl)
    complete = [row for row in rows if row["full_site_verified"]]
    incomplete = [row for row in rows if not row["full_site_verified"]]
    report = {
        "run_root": str(run_root),
        "final_jsonl": args.final_jsonl,
        "final_jsonl_records": final_records,
        "site_count": len(rows),
        "full_site_verified_count": len(complete),
        "incomplete_site_count": len(incomplete),
        "all_sites_full_verified": len(rows) > 0 and len(incomplete) == 0,
        "incomplete_sites": [
            {
                "seed_url": row["seed_url"],
                "stopped_reason": row["stopped_reason"],
                "remaining_frontier": row["remaining_frontier"],
                "summary_path": row["summary_path"],
            }
            for row in incomplete[:100]
        ],
    }
    Path(args.output_json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with Path(args.output_jsonl).open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
