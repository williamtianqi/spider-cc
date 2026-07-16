#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <seed-url> <output-dir> [allowed-domain-or-host]" >&2
  exit 1
fi

SEED_URL="$1"
OUTPUT_DIR="$2"
SCOPE="${3:-}"

ARGS=(
  python3 src/pipeline_domain_crawler.py
  --seed-url "$SEED_URL"
  --output-dir "$OUTPUT_DIR"
  --max-pages 500000
  --max-discovered 2000000
  --max-depth 50
  --max-sitemaps 2000
  --fetch-workers 256
  --extract-workers 1
  --max-host-workers 96
  --max-in-flight-fetch 8192
  --max-in-flight-extract 1
  --progress-interval 1000
  --extract-mode regex-inline
  --link-mode regex
  --robots-check-stage fetch
  --no-write-links
  --timeout 12
  --max-page-bytes 2000000
  --max-sitemap-bytes 50000000
)

if [[ -n "$SCOPE" ]]; then
  ARGS+=(--allowed-domain "$SCOPE" --include-subdomains)
fi

"${ARGS[@]}"
