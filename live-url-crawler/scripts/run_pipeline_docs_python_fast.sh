#!/usr/bin/env bash
set -euo pipefail

python3 src/pipeline_domain_crawler.py \
  --seed-url https://docs.python.org/3/ \
  --allowed-host docs.python.org \
  --output-dir data/runs/pipeline_docs_python_fast \
  --max-pages 5000 \
  --max-discovered 300000 \
  --max-depth 30 \
  --max-sitemaps 500 \
  --fetch-workers 128 \
  --extract-workers 8 \
  --max-host-workers 64 \
  --max-in-flight-fetch 2048 \
  --max-in-flight-extract 1024 \
  --progress-interval 100 \
  --timeout 15 \
  --max-page-bytes 2000000 \
  --max-sitemap-bytes 50000000
