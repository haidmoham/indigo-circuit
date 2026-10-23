# Indigo Circuit

A competitive Pokémon TCG field guide: tournament results, player ratings, archetype history, and experimental deck estimates.

**[Open Indigo Circuit](https://indigocircuit.app)**

## What runs

Python ingests online events from the Limitless API and major events from Limitless Labs into DuckDB. dbt builds staging views and analytics marts; Flask serves rankings, player records, and the Tech Lab. The interface is vanilla JavaScript and CSS.

- [`ingest/load.py`](ingest/load.py): online standings, pairings, and decklists; incremental by tournament. The default scan is three pages, not the entire historical catalog.
- [`ingest/labs.py`](ingest/labs.py): discovery and scraping for major events, including current-rotation decklists.
- [`ingest/glicko.py`](ingest/glicko.py): Glicko-2 ratings.
- [`transform/models/`](transform/models/): player, archetype, matchup, card, and seasonal marts.
- [`dashboard/app.py`](dashboard/app.py): routes and the legacy scheduler; the read-only deployment entrypoint disables scheduling.
- [`dashboard/pipeline_gate.py`](dashboard/pipeline_gate.py): validation before shadow-database promotion.
- [`dashboard/ev.py`](dashboard/ev.py): empirical-Bayes shrinkage for experimental deck estimates.

The scheduler normally rebuilds a shadow copy, validates its marts, then promotes it with `os.replace`. Rejected shadows preserve the live database. **First boot or a failed shadow copy falls back to direct writes to the live database; that path does not have the same validation and rollback guarantee.** The standalone pipeline script also writes to its configured database directly.

The proposed free Vercel Hobby deployment serves a private immutable DuckDB snapshot. It is prepared in source; production deployment and domain cutover need separate verification. Daily refresh is not wired. See [free hosting recovery](docs/FREE_HOSTING.md) for setup, gating, and freshness limits. Historical `railway*.toml` files describe the previous host and are not restoration instructions. `ptcg_scouting` remains the internal dbt project/profile name.

## Local setup

Use Python in a virtual environment. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-ingest.txt
cp .env.example .env
mkdir -p data
export DUCKDB_PATH="$PWD/data/ptcg.duckdb"
export DBT_MARTS_SCHEMA=dbt_dev_marts
```

The exported absolute path makes the Python loaders and dbt use the same file even when their working directories differ. Limitless tournament data is available without an API key; source availability and rate limits can change. Keep any optional ingestion credentials outside the deployment bundle.

Inspect source access without database writes:

```bash
python ingest/load.py --dry-run --pages 1
python ingest/labs.py --help
```

To populate your **local** database and build the marts:

```bash
bash ingest/pipeline.sh
```

This can take substantial time and makes requests to the external data sources. To rebuild existing local data only, use `bash ingest/pipeline.sh --dbt-only`. The script creates a temporary dbt profile and runs `dbt deps` and `dbt run`; it does not run `dbt test` or the shadow-promotion gate.

Run the dashboard:

```bash
python -m flask --app app run --port 5001
```

Open `http://localhost:5001`. This entrypoint disables the scheduler and prewarm thread. The dashboard needs a populated database, including raw tables and marts; a fresh empty database is not a demo dataset. `/api/health` reports missing, broken, or stale data separately.

## Checks

These checks use fixtures or source files, without running production ingest:

```bash
python scripts/test_pipeline_gate.py
node scripts/test_champion_presentation.mjs
```

The gate suite covers a healthy shadow, a legitimate empty-major-data state, and six rejected failure fixtures. To run dbt schema tests against a populated local database:

```bash
cp transform/profiles.yml.example transform/profiles.yml
(cd transform && dbt deps --profiles-dir . && dbt test --profiles-dir .)
rm transform/profiles.yml
```

## Interpretation and limits

The Tech Lab estimates associations in observed match data, not the causal effect of adding a card. Sparse samples shrink toward matchup priors. The displayed 90% intervals are approximate model intervals: they exclude baseline uncertainty and dependence between cards. Archetype detection can be uncertain for hybrid lists; manual selection is available.

Source coverage, name-based major-event joins, and current-rotation filters constrain the analysis. Read the implementation and visible source/sample disclosures before treating an estimate as a competitive edge.

[Project context](CONTEXT.md) records operational details. [Portfolio](https://mhaider.dev) connects this project to the rest of the work.
