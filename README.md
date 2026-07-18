# Spider-CC

Common Crawl + live web crawler for collecting fresh, quality web content.

This repository is an experimental data pipeline that seeds URLs from the [Common Crawl](https://commoncrawl.org/) WET/CDX corpus, normalizes/deduplicates sites by eTLD+1, and then runs a live crawl to extract structured text.

## What's inside

- `live-url-crawler/` — the main crawler package
  - Three crawl engines: turbo (`pipeline_domain_crawler.py`), async (`async_pipeline_crawler.py`), and multi-process async (`multiproc_async_crawler.py`)
  - Common Crawl index/WET ingestion, site discovery and URL seeding
  - HTTP conditional request caching (ETag/Last-Modified, 304 handling)
  - Anti-bot mitigation: `curl_cffi` TLS/JA3/JA4 fingerprint impersonation, retry/backoff with `Retry-After`, challenge-page detection, and `likely_blocked` site marking
  - Fallback to rotated real-browser user-agents when `curl_cffi` is unavailable
  - Python 3.8/3.9/3.10+ compatible

See [`live-url-crawler/README.md`](live-url-crawler/README.md) for detailed usage, architecture and current status.

## Quick start

```bash
cd live-url-crawler
python3 -m pip install -r requirements.txt

# Async engine with TLS fingerprint impersonation
python3 src/async_pipeline_crawler.py \
  --seeds-tsv seeds.tsv \
  --output-root output/ \
  --max-sites 10 \
  --pages-per-site 50 \
  --max-concurrent 128

# Or use the provided convenience scripts
bash scripts/run_async_crawl_10x.sh seeds.tsv output/ --impersonate
```

## Project status

| Module | Status |
| -------- | -------- |
| Common Crawl site discovery & URL seeding | Implemented |
| eTLD+1 domain normalization & dedup | Implemented |
| Turbo / async / multi-process async crawl engines | Implemented |
| Anti-bot: retry/backoff, challenge detection, likely-blocked marking | Implemented |
| TLS/JA3/JA4 fingerprint impersonation (`curl_cffi`) | Implemented |
| HTTP conditional caching (304, ETag/Last-Modified) | Implemented |
| Unit tests + CI lint/test | Implemented |
| Distributed/petascale 200B-token crawl | Design only |

See `live-url-crawler/README.md` and `PLAN_200B.md` for the full roadmap and scale analysis.

## Requirements

- Python >= 3.8
- Core: `trafilatura>=1.12.0`, `lxml_html_clean>=0.1.0`
- Async: `aiohttp>=3.9.0`
- Anti-bot TLS impersonation (optional): `curl_cffi>=0.9.0`

## License

MIT (or add your license here)
