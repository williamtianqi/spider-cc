#!/usr/bin/env bash
set -euo pipefail

python3 src/domain_link_crawler.py \
  --seed-url https://docs.python.org/3/ \
  --allowed-host docs.python.org \
  --output-dir data/runs/domain_docs_python_small \
  --max-pages 120 \
  --max-discovered 5000 \
  --max-depth 4 \
  --max-sitemaps 50 \
  --max-workers 4 \
  --batch-size 30 \
  --timeout 25 \
  --max-page-bytes 2000000 \
  --max-sitemap-bytes 50000000
