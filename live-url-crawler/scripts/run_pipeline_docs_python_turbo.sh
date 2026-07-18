#!/usr/bin/env bash
set -euo pipefail

python3 src/pipeline_domain_crawler.py \
  --seed-url https://docs.python.org/3/ \
  --allowed-host docs.python.org \
  --output-dir data/runs/pipeline_docs_python_turbo \
  --max-pages 5000 \
  --max-discovered 300000 \
  --max-depth 30 \
  --max-sitemaps 500 \
  --fetch-workers 256 \
  --extract-workers 1 \
  --max-host-workers 96 \
  --max-in-flight-fetch 4096 \
  --max-in-flight-extract 1 \
  --progress-interval 100 \
  --extract-mode regex-inline \
  --link-mode regex \
  --robots-check-stage fetch \
  --no-write-links \
  --timeout 12 \
  --max-page-bytes 2000000 \
  --max-sitemap-bytes 50000000
