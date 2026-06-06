#!/usr/bin/env bash
# Indigo Circuit data pipeline
#
# Usage:
#   bash ingest/pipeline.sh            # full run (all 4 steps)
#   bash ingest/pipeline.sh --dbt-only # only regenerate dbt marts
#
# Requires env var: DUCKDB_PATH (default: data/ptcg.duckdb)
set -euo pipefail

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

DBT_ONLY=0
for arg in "$@"; do
  [[ "$arg" == "--dbt-only" ]] && DBT_ONLY=1
done

log "=== Indigo Circuit pipeline start (dbt-only=$DBT_ONLY) ==="

DUCKDB_PATH="${DUCKDB_PATH:-/data/ptcg.duckdb}"

if [[ $DBT_ONLY -eq 0 ]]; then
  log "--- Step 1/4: Limitless Labs ingest (auto-discover + current rotation) ---"
  python3 ingest/labs.py --discover --with-decklists --current-rotation

  log "--- Step 2/4: Limitless API ingest ---"
  python3 ingest/load.py

  log "--- Step 3/4: Glicko-2 ratings ---"
  python3 ingest/glicko.py
fi

log "--- dbt run ---"
cat > transform/profiles.yml <<EOF
ptcg_scouting:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: "${DUCKDB_PATH}"
      schema: dbt_dev
      threads: 4
EOF

cd transform
PATH="/opt/venv/bin:$PATH" dbt deps --profiles-dir . --target dev
PATH="/opt/venv/bin:$PATH" dbt run --profiles-dir . --target dev
cd ..

rm -f transform/profiles.yml

log "=== Pipeline complete ==="
