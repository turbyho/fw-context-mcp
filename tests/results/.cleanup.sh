#!/usr/bin/env bash
# Clean up old test-run result JSONs, keeping the N most recent.
# Usage: ./tests/results/.cleanup.sh [keep_count]
#   keep_count: number of most-recent run files to keep (default: 5)
set -euo pipefail

RESULTS_DIR="$(cd "$(dirname "$0")" && pwd)"
KEEP=${1:-5}

cd "$RESULTS_DIR"

# List test_run_*.json files sorted by modification time (oldest first),
# skip the newest $KEEP, delete the rest.
find . -maxdepth 1 -name 'test_run_*.json' -printf '%T@ %p\n' \
  | sort -n \
  | head -n -"$KEEP" \
  | cut -d' ' -f2- \
  | while IFS= read -r f; do
      echo "Removing: $f"
      rm "$f"
    done

echo "Kept newest $KEEP result files."
