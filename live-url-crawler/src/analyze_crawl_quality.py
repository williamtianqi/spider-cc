#!/usr/bin/env python3
import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path):
    rows = []
    p = Path(path)
    if not p.exists():
        return rows
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def read_csv_rows(path):
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def percentile(values, ratio):
    if not values:
        return 0
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * ratio))))
    return values[index]


def top_counter(counter, limit=20):
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def analyze_run(run_dir):
    run_dir = Path(run_dir)
    manifest = read_jsonl(run_dir / "manifest.jsonl")
    extracted = read_jsonl(run_dir / "extracted_text.jsonl")
    if not extracted:
        extracted = read_jsonl(run_dir / "pages.jsonl")
    discovered = read_csv_rows(run_dir / "discovered_urls.csv")
    if not discovered:
        discovered = read_jsonl(run_dir / "discovered_urls.jsonl")
    links = read_csv_rows(run_dir / "links.csv")
    if not links:
        links = read_jsonl(run_dir / "links.jsonl")
    frontier = read_jsonl(run_dir / "frontier_remaining.jsonl")
    sitemap_reports = read_jsonl(run_dir / "sitemap_reports.jsonl")

    status_counter = Counter(str(row.get("status", "")) for row in manifest)
    content_type_counter = Counter(row.get("content_type", "") for row in manifest)
    error_counter = Counter(row.get("error", "") for row in manifest if row.get("error"))
    source_counter = Counter(row.get("source", "") for row in discovered)
    depth_counter = Counter(str(row.get("depth", "")) for row in discovered)
    extracted_source_counter = Counter(row.get("source", "") for row in extracted)

    ok_manifest = [row for row in manifest if row.get("ok")]
    manifest_text_lengths = [int(row.get("text_length") or 0) for row in ok_manifest]
    manifest_non_empty_text = [value for value in manifest_text_lengths if value > 0]
    manifest_duplicate_content_count = sum(1 for row in manifest if row.get("content_duplicate"))
    text_lengths = [int(row.get("text_length") or 0) for row in extracted]
    non_empty_text = [value for value in text_lengths if value > 0]
    content_hashes = [row.get("content_hash") for row in extracted if row.get("content_hash")]
    duplicate_content_count = len(content_hashes) - len(set(content_hashes))
    url_hashes = [row.get("url_hash") for row in discovered if row.get("url_hash")]
    duplicate_url_count = len(url_hashes) - len(set(url_hashes))

    quality = {
        "run_dir": str(run_dir),
        "counts": {
            "discovered_urls": len(discovered),
            "link_edges": len(links),
            "manifest_records": len(manifest),
            "ok_manifest_records": len(ok_manifest),
            "manifest_non_empty_text_records": len(manifest_non_empty_text),
            "extracted_unique_text_records": len(extracted),
            "manifest_duplicate_content_records": manifest_duplicate_content_count,
            "remaining_frontier": len(frontier),
            "sitemap_reports": len(sitemap_reports),
        },
        "rates": {
            "fetch_success_rate": round(len(ok_manifest) / len(manifest), 4) if manifest else 0,
            "manifest_non_empty_text_rate": round(len(manifest_non_empty_text) / len(ok_manifest), 4) if ok_manifest else 0,
            "unique_text_keep_rate": round(len(extracted) / len(ok_manifest), 4) if ok_manifest else 0,
            "manifest_duplicate_content_rate": round(manifest_duplicate_content_count / len(ok_manifest), 4) if ok_manifest else 0,
            "output_non_empty_text_rate": round(len(non_empty_text) / len(extracted), 4) if extracted else 0,
            "url_duplicate_rate_after_output": round(duplicate_url_count / len(url_hashes), 4) if url_hashes else 0,
            "content_duplicate_rate_after_output": round(duplicate_content_count / len(content_hashes), 4) if content_hashes else 0,
        },
        "unique_output_text_length": {
            "min": min(text_lengths) if text_lengths else 0,
            "p25": percentile(text_lengths, 0.25),
            "p50": percentile(text_lengths, 0.50),
            "p75": percentile(text_lengths, 0.75),
            "p90": percentile(text_lengths, 0.90),
            "p95": percentile(text_lengths, 0.95),
            "max": max(text_lengths) if text_lengths else 0,
            "avg": round(sum(text_lengths) / len(text_lengths), 2) if text_lengths else 0,
            "thin_text_lt_200": sum(1 for value in text_lengths if value < 200),
            "good_text_ge_500": sum(1 for value in text_lengths if value >= 500),
            "long_text_ge_2000": sum(1 for value in text_lengths if value >= 2000),
        },
        "manifest_text_length": {
            "min": min(manifest_text_lengths) if manifest_text_lengths else 0,
            "p25": percentile(manifest_text_lengths, 0.25),
            "p50": percentile(manifest_text_lengths, 0.50),
            "p75": percentile(manifest_text_lengths, 0.75),
            "p90": percentile(manifest_text_lengths, 0.90),
            "p95": percentile(manifest_text_lengths, 0.95),
            "max": max(manifest_text_lengths) if manifest_text_lengths else 0,
            "avg": round(sum(manifest_text_lengths) / len(manifest_text_lengths), 2) if manifest_text_lengths else 0,
            "thin_text_lt_200": sum(1 for value in manifest_text_lengths if value < 200),
            "good_text_ge_500": sum(1 for value in manifest_text_lengths if value >= 500),
            "long_text_ge_2000": sum(1 for value in manifest_text_lengths if value >= 2000),
        },
        "top": {
            "status": top_counter(status_counter),
            "content_type": top_counter(content_type_counter),
            "errors": top_counter(error_counter),
            "discovered_source": top_counter(source_counter),
            "discovered_depth": top_counter(depth_counter),
            "extracted_source": top_counter(extracted_source_counter),
        },
        "samples": {
            "extracted_titles": [
                {"url": row.get("url", ""), "title": row.get("title", ""), "text_length": row.get("text_length", 0)}
                for row in extracted[:20]
            ],
            "errors": [row for row in manifest if row.get("error")][:20],
        },
    }
    return quality


def main():
    parser = argparse.ArgumentParser(description="Analyze domain crawler output quality")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    result = analyze_run(args.run_dir)
    output = args.output or str(Path(args.run_dir) / "quality_report.json")
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
