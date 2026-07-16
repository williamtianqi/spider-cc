#!/usr/bin/env bash
set -euo pipefail

python3 src/domain_link_crawler.py \
  --seed-url https://docs.python.org/3/ \
  --allowed-host docs.python.org \
  --output-dir data/runs/domain_docs_python_fast \
  --max-pages 5000 \
  --max-discovered 300000 \
  --max-depth 30 \
  --max-sitemaps 500 \
  --max-workers 32 \
  --max-host-workers 16 \
  --batch-size 500 \
  --progress-interval 100 \
  --timeout 20 \
  --max-page-bytes 2000000 \
  --max-sitemap-bytes 50000000
