#!/usr/bin/env bash
# Nightly Indigo Circuit data pipeline
# Step 1 — Limitless Labs (official events: Regionals, ICs, Worlds)
# Step 2 — Limitless API  (community/online events)
# Step 3 — Glicko-2 ratings
# Step 4 — dbt run (rebuild all Snowflake marts)
#
# Requires env vars: SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD,
#   SNOWFLAKE_ROLE (default SYSADMIN), SNOWFLAKE_DATABASE (default PTCG_SCOUTING),
#   SNOWFLAKE_WAREHOUSE (default COMPUTE_WH)
set -euo pipefail

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

log "=== Indigo Circuit nightly pipeline start ==="

# ── Step 1: Limitless Labs ────────────────────────────────────────────────────
log "--- Step 1/4: Limitless Labs ingest ---"
python3 ingest/labs.py

# ── Step 2: Limitless API ─────────────────────────────────────────────────────
log "--- Step 2/4: Limitless API ingest ---"
python3 ingest/load.py

# ── Step 3: Glicko-2 ratings ──────────────────────────────────────────────────
log "--- Step 3/4: Glicko-2 ratings ---"
python3 ingest/glicko.py

# ── Step 4: dbt run ───────────────────────────────────────────────────────────
log "--- Step 4/4: dbt run ---"

# Generate profiles.yml at runtime from env vars (file is gitignored).
# Schema 'dbt_dev' produces dbt_dev_marts in Snowflake — what the dashboard reads.
cat > transform/profiles.yml <<EOF
ptcg_scouting:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: "${SNOWFLAKE_ACCOUNT}"
      user: "${SNOWFLAKE_USER}"
      password: "${SNOWFLAKE_PASSWORD}"
      role: "${SNOWFLAKE_ROLE:-SYSADMIN}"
      database: "${SNOWFLAKE_DATABASE:-PTCG_SCOUTING}"
      warehouse: "${SNOWFLAKE_WAREHOUSE:-COMPUTE_WH}"
      schema: dbt_dev
      threads: 4
EOF

cd transform
dbt run --profiles-dir . --target dev
cd ..

rm -f transform/profiles.yml

log "=== Pipeline complete ==="
