#!/usr/bin/env bash
set -euo pipefail

python3 src/optimized_live_crawler.py \
  --base-url https://docs.python.org/3/ \
  --allowed-host docs.python.org \
  --include-path-prefix /3/ \
  --output-dir data/runs/docs_python_org \
  --max-sitemaps 30 \
  --max-candidates 1000 \
  --link-discovery-pages 20 \
  --max-pages 80 \
  --max-workers 4 \
  --timeout 25 \
  --max-page-bytes 2000000 \
  --max-sitemap-bytes 50000000
