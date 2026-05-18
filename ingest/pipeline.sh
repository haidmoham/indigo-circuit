#!/usr/bin/env bash
# Nightly Indigo Circuit data pipeline
# Step 1 — Limitless Labs (official events: Regionals, ICs, Worlds)
# Step 2 — Limitless API  (community/online events)
# Step 3 — Glicko-2 ratings
# Step 4 — dbt run (rebuild all DuckDB marts)
#
# Requires env var: DUCKDB_PATH (default: data/ptcg.duckdb)
set -euo pipefail

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

log "=== Indigo Circuit nightly pipeline start ==="

log "--- Step 1/4: Limitless Labs ingest ---"
python3 ingest/labs.py

log "--- Step 2/4: Limitless API ingest ---"
python3 ingest/load.py

log "--- Step 3/4: Glicko-2 ratings ---"
python3 ingest/glicko.py

log "--- Step 4/4: dbt run ---"
DUCKDB_PATH="${DUCKDB_PATH:-/data/ptcg.duckdb}"

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
dbt run --profiles-dir . --target dev
cd ..

rm -f transform/profiles.yml

log "=== Pipeline complete ==="
