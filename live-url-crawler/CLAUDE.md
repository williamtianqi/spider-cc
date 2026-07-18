# Claude Code Handoff

## User Objective

The user wants to build a high-throughput live web crawling system that uses Common Crawl only for large-scale site discovery, then crawls the discovered sites live and stores extracted page content as local JSONL files.

The final goal is not to parse Common Crawl WARC/WET content as the dataset. Common Crawl is only the seed source. The actual pages must be fetched live from the public web, extracted locally, and written to reviewable JSONL files in the project root.

## Core Goal

Increase live crawling throughput by roughly 10x to 100x compared with the original small parallel crawl, while keeping full page content output.

The user ultimately wants to estimate and approach very large-scale coverage, potentially tens of billions of live pages, by:

1. Sampling many Common Crawl WET path headers.
2. Extracting high-quality English-friendly domains from `WARC-Target-URI`.
3. Launching many full-site live crawls in parallel.
4. Monitoring live throughput and completion without blocking on huge files.
5. Saving crawled page records into JSONL files in the current project directory.

## Important Clarification

Do not treat Common Crawl as the content source.

Correct flow:

```text
Common Crawl WET headers
  -> discover domains / seed URLs
  -> live crawl each discovered site
  -> extract page title/text/metadata from live HTML
  -> write pages.jsonl per site
  -> incrementally merge into root-level JSONL for inspection
```

Incorrect flow:

```text
Common Crawl WET/WARC content
  -> extract text directly
  -> use that as final dataset
```

The user explicitly wants the live-crawled output, not Common Crawl text extraction.

## What Counts as Success

A site is considered fully crawled only when its site summary has:

```json
{
  "site_crawl_complete": true,
  "stopped_reason": "frontier_exhausted"
}
```

If a site stops because of `max_pages_reached` or `max_discovered_reached`, it is capped/incomplete and should not be counted as a fully crawled site.

## Output Requirement

The user wants easy-to-review JSONL files in the project root, especially files like:

```text
cc_live_pages_100x_partial.jsonl
cc_live_pages_100x_en_partial.jsonl
cc_live_run_stats_100x.jsonl
cc_live_run_latest_100x.json
cc_site_discovery_summary_100x_en.json
cc_site_discovery_progress_100x_en.json
```

The partial JSONL should grow while the crawl is running. Final JSONL files can be produced after the crawl finishes.

## Performance Requirement

The system should avoid operations that block on large JSONL files.

Do not monitor progress by repeatedly running `wc -l` or scanning huge `pages.jsonl` files.

Use lightweight progress summaries instead:

- `progress.json`
- `summary.json`
- `cc_live_run_latest_*.json`
- `cc_live_run_stats_*.jsonl`
- file size / mtime checks
- process counts

The monitor should use incremental partial merging by file offsets, not full rebuilds.

## Current Architecture

### Discovery

`src/common_crawl_site_discovery.py` reads Common Crawl WET paths, opens WET files, parses WARC headers, and extracts `WARC-Target-URI` values to derive live site seeds.

Important features:

- `--english-domain-only`
- `--spread-wet-paths`
- `--progress-json`
- invalid host filtering
- spam/adult term filtering
- TLD summary stats
- per-WET manifest rows

### Live Crawl

`src/pipeline_domain_crawler.py` performs live site crawling with:

- sitemap discovery
- robots handling
- live HTML fetching
- regex-inline content extraction
- regex link extraction
- per-site `pages.jsonl`
- per-site `progress.json`
- per-site `summary.json`

### Multi-site Runner

`scripts/run_pipeline_multi_seed_turbo.sh` runs many site crawlers in parallel from a TSV seed file.

It expects TSV columns:

```text
seed_url<TAB>scope_domain_or_host<TAB>output_name
```

### Monitoring

`src/monitor_cc_live_run.py` writes minute-level stats and optionally merges partial JSONL incrementally.

`src/check_cc_live_status.py` is the safe lightweight status checker.

## Current Preferred Run Shape

For English large-sample discovery and 100x-style live crawling, use a run like:

```bash
/Users/tiliu4/.pyenv/versions/3.11.6/bin/python3 src/common_crawl_site_discovery.py \
  --crawl-id CC-MAIN-2025-08 \
  --max-wet-files 1000 \
  --spread-wet-paths \
  --max-sites 20000 \
  --max-sites-per-wet 20000 \
  --max-records-per-wet 200000 \
  --workers 8 \
  --timeout 75 \
  --english-domain-only \
  --output-sites-jsonl cc_sites_100x_en.jsonl \
  --output-seeds-tsv cc_seed_sites_100x_en.tsv \
  --output-manifest-jsonl cc_site_discovery_manifest_100x_en.jsonl \
  --summary-json cc_site_discovery_summary_100x_en.json \
  --progress-json cc_site_discovery_progress_100x_en.json
```

Then run live crawling with:

```bash
FETCH_WORKERS=32 \
MAX_HOST_WORKERS=16 \
MAX_IN_FLIGHT_FETCH=1024 \
TIMEOUT_SECONDS=8 \
bash scripts/run_pipeline_multi_seed_turbo.sh \
  cc_seed_sites_100x_en.tsv \
  128 \
  100000 \
  data/runs/cc_live_sites_100x_en
```

Then monitor with:

```bash
/Users/tiliu4/.pyenv/versions/3.11.6/bin/python3 src/monitor_cc_live_run.py \
  --run-root data/runs/cc_live_sites_100x_en \
  --partial-jsonl cc_live_pages_100x_en_partial.jsonl \
  --stats-jsonl cc_live_run_stats_100x_en.jsonl \
  --latest-json cc_live_run_latest_100x_en.json \
  --merge-partial \
  --interval-seconds 60 \
  --target-records-per-minute 50000
```

## Operational Notes

Use the pyenv Python path above for Common Crawl discovery. Homebrew Python 3.14 previously failed SSL verification when downloading Common Crawl metadata.

Common Crawl WET servers may return 503 or time out. This does not mean live crawling failed; it only means that specific WET seed-source files failed during discovery.

For better statistical sampling, prefer `--spread-wet-paths` so discovery samples across the full WET path list instead of getting stuck in one segment.

## User Preference

The user wants direct execution and practical results, not only suggestions.

When debugging crawler progress:

1. Confirm process counts.
2. Check latest/progress JSON.
3. Check file size and mtime.
4. Avoid scanning large JSONL files.
5. Restart cleanly if the run is genuinely stuck.

## One-sentence Summary

Build a fast, English-prioritized, Common-Crawl-seeded live crawler that discovers many real sites from Common Crawl metadata, crawls those sites live until frontier exhaustion where possible, writes full extracted page records to root-level JSONL files, and provides non-blocking monitoring/statistics for scaling estimates toward tens of billions of pages.
