#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <seed-url> <output-dir> [allowed-host-or-domain]" >&2
  exit 1
fi

SEED_URL="$1"
OUTPUT_DIR="$2"
SCOPE="${3:-}"

ARGS=(
  python3 src/domain_link_crawler.py
  --seed-url "$SEED_URL"
  --output-dir "$OUTPUT_DIR"
  --max-pages 1000
  --max-discovered 100000
  --max-depth 10
  --max-sitemaps 200
  --max-workers 32
  --max-host-workers 16
  --batch-size 500
  --progress-interval 100
  --timeout 20
  --max-page-bytes 2000000
  --max-sitemap-bytes 50000000
)

if [[ -n "$SCOPE" ]]; then
  ARGS+=(--allowed-domain "$SCOPE" --include-subdomains)
fi

"${ARGS[@]}"
