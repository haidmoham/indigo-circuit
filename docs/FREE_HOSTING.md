# Free hosting recovery

The Vercel Hobby migration is prepared in source. This document does not establish that a deployment is live or that `indigocircuit.app` has been cut over. Confirm the deployment, API results, source dates, and domain separately.

## Serving design

- Root `app.py` exports the Flask application for Vercel and disables the embedded ingest scheduler and cache-prewarm thread before import.
- Leave `ADMIN_SECRET` unset. The administrative ingest endpoints then reject requests with HTTP 403. Request workers serve an immutable snapshot; they do not refresh it.
- `requirements.txt` contains serving dependencies. `requirements-ingest.txt` adds dbt and Beautiful Soup for a separate ingestion runner.
- The snapshot is bundled privately as `data/ptcg.duckdb`. An upload may instead contain `data/ptcg.duckdb.gz`; the build decompresses a candidate and promotes it only after validation. When both inputs exist, the gzip is the replacement candidate, so regenerate it after every rebuild.
- `scripts/build_vercel.py` checks required raw/mart tables and the existing production gate before preparing `public/static`. A missing, locked, corrupt, or rejected database fails the build. Stale but valid data emits a warning and remains distinguishable through health.
- Only static artwork, CSS, scripts, and fonts belong under `public/`. Never publish the database through a public Git repository, CDN, release asset, or download route. Database, WAL, and gzip files are gitignored.

Keep the complete database, including `raw.tournaments`, `raw.standings`, `raw.matches`, `raw.decklists`, `raw.glicko_ratings`, and all four `raw.major_*` tables. The application queries raw results as well as dbt marts; a marts-only snapshot breaks player, card, and EV paths.

Use the existing verified Hobby account without paid additions. The standard Python function bundle limit is 500 MB uncompressed, including dependencies and data; Hobby provides 2 GB memory and a maximum 300-second invocation. This project configures 60 seconds per request. Ingestion is too long for a request worker. Do not rely on beta limits to make a bundle fit. See the official [Flask deployment guide](https://vercel.com/docs/frameworks/backend/flask) and [function limits](https://vercel.com/docs/functions/limitations).

## Recover a local candidate

Use Python 3.12 and a Bash-capable Linux/WSL runner for the pipeline. Keep a previous good snapshot intact while rebuilding another file.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-ingest.txt
mkdir -p data
export DUCKDB_PATH="$PWD/data/ptcg_candidate.duckdb"
export DBT_MARTS_SCHEMA=dbt_dev_marts
bash ingest/pipeline.sh
python scripts/verify_ingest.py --db "$DUCKDB_PATH"
```

The pipeline writes to its configured candidate directly. Its success alone does not authorize promotion. `verify_ingest.py` must print `VERIFY_JSON` with `ok: true`; when a previous snapshot exists, also call `dashboard.pipeline_gate.validate_shadow(candidate_path, previous_path)` to check regression against it. Preserve that previous file until the replacement passes. Stop after three failed recovery iterations and diagnose the failing invariant, as specified in the [ingest loop](../skills/ingest-loop/SKILL.md).

The default online scan covers three index pages and events with at least 64 players. Major-event decklist coverage is normally top 64. A new rebuild does not recreate all historical or full-field coverage automatically. The [Limitless API documentation](https://docs.limitlesstcg.com/developer.html) permits tournament data access without an API key; source availability and rate limits still apply. Do not generate a credential to work around a source error.

After validation, close all writers and checkpoint the candidate before preparing the private deployment snapshot at `data/ptcg.duckdb`. Keep ingestion configuration and credentials outside the uploaded source. Then run:

```bash
export DUCKDB_PATH="$PWD/data/ptcg.duckdb"
python scripts/build_vercel.py
python -m flask --app app run --port 5001
```

The build prints `SNAPSHOT_HEALTH`. Inspect API payloads and the visible interface before changing DNS. Verify rankings, online results, player profiles, meta, card scouting, and EV responses against the same snapshot; HTML 200 alone does not prove that these paths work.

## Health and freshness

`GET /api/health` returns HTTP 200 only for a structurally valid snapshot whose newest stored results are within `DATA_MAX_AGE_HOURS` (default 72). Missing, error, incomplete, and stale states return HTTP 503 with distinct `status`/`reason` fields. `available: true` with `status: stale` means a validated older snapshot can still serve queries.

Each source reports its latest covered event and latest result load time. Load time is not proof that the source was successfully checked today or that every available event was ingested. Responses state that limitation explicitly. Health computation is cached in-process for 30 seconds; responses use `Cache-Control: no-store` and include `checked_at`.

**Daily refresh is not wired.** A successful deployment serves the bundled snapshot until another validated snapshot is deployed. Do not describe that data as continuously current. A future refresh job needs its own bounded ingestion, checker, private artifact transfer, and deployment verification using existing authorized access.

## Recovery data checks

The October 23 recovery combined the historical May snapshot with 85 freshly ingested eligible online events and top-64 match/decklist data for all 18 current-rotation majors discovered at the source. The combined database retains 600 tournament index entries, 17,192 online standings, and 37,464 major standings. The latest covered online event is October 22; the latest major is Nice on October 18. Historical online coverage between the May backup and the recent three-page scan is incomplete.

All 22 dbt models and the production pipeline gate pass. The 60 dbt schema tests expose one data limitation: 4,504 online standings have a null placing, and all 4,504 have a recorded drop round. These nulls are preserved from the source. The quoted `placing` test now runs and reports this issue; 59 other schema tests pass. As specified by the ingest-loop skill, these schema tests remain a soft signal and do not replace the production gate.

## Verification recorded during preparation

These existing checks passed: 8 pipeline-gate cases, 4 social-preview checks, and 3 champion-presentation checks.

```bash
python scripts/test_pipeline_gate.py
python scripts/test_social_preview.py
node scripts/test_champion_presentation.mjs
```

Manual Flask checks confirmed root HTML 200, missing-database health 503 with `no-store`, and unauthenticated admin ingest 403. The read-only entrypoint preserved an existing pipeline signal and started no prewarm thread. The build refused a locked/unavailable snapshot. These checks do not establish a successful production deployment or current source coverage.

Historical `railway*.toml` files describe the previous host only. They are not restoration instructions; this recovery must not use Railway.
