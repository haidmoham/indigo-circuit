# PTCG Scouting Report

Player performance analytics for competitive Pokémon TCG. Search any player to see their career stats, archetype history, consistency score, and head-to-head record.

**Live demo:** [railway URL — add after deploy]

---

## Architecture

```
Limitless TCG API (play.limitlesstcg.com/api)
        ↓
  ingest/load.py  (Python + snowflake-connector)
        ↓
  Snowflake: PTCG_SCOUTING.RAW.PTCG
  ├── TOURNAMENTS
  ├── STANDINGS
  └── MATCHES
        ↓
  dbt (transform/)
  ├── staging/  — clean + type-cast raw tables (materialized as views)
  │   ├── stg_tournaments
  │   ├── stg_standings
  │   └── stg_matches
  └── marts/    — player analytics (materialized as tables)
      ├── player_tournament_history   ← foundation model
      ├── players                     ← career stats + consistency score
      ├── player_archetype_history    ← deck loyalty + win rate per archetype
      └── player_vs_player            ← head-to-head records
        ↓
  Flask + Plotly.js dashboard (dashboard/)
        ↓
  Railway (deployed)
```

---

## Setup

### 1. Snowflake trial account
Sign up at snowflake.com — 30-day free trial, $400 credit. Note your **account identifier** (e.g. `abc12345.us-east-1`).

### 2. Environment
```bash
cp .env.example .env
# fill in SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD
```

### 3. dbt profile
```bash
cp transform/profiles.yml.example ~/.dbt/profiles.yml
# values are read from .env automatically via env_var()
```

### 4. Install dependencies
```bash
pip install -r requirements.txt
cd transform && dbt deps   # installs dbt_utils
```

### 5. Limitless API key (optional — needed for deck lists only)
Request at https://play.limitlesstcg.com/account/settings/api.  
Add to `.env` as `LIMITLESS_API_KEY`. Standings + pairings work without it.

### 6. Ingest
```bash
python ingest/load.py --dry-run   # verify API connectivity
python ingest/load.py             # load all PTCG tournaments into Snowflake
```

### 7. Transform
```bash
cd transform
dbt run
dbt test
dbt docs generate && dbt docs serve   # view lineage graph
```

### 8. Dashboard
```bash
cd dashboard
python app.py   # http://localhost:5001
```

---

## Key dbt concepts demonstrated

| Concept | Where |
|---|---|
| Source definitions + freshness | `models/staging/_sources.yml` |
| Staging / mart layer separation | `models/staging/` vs `models/marts/` |
| Incremental-ready foundation model | `player_tournament_history.sql` |
| Window functions (loyalty %) | `player_archetype_history.sql` |
| Fan-out + aggregate pattern | `player_vs_player.sql` |
| Schema tests (not_null, unique) | `_sources.yml`, `_stg_models.yml`, `_mart_models.yml` |
| dbt docs lineage graph | `dbt docs generate` |

---

## Data source

Tournament data via the [Limitless TCG API](https://play.limitlesstcg.com/api) — standings and pairings available without auth. Covers online Limitless events and select in-person tournaments.

**Planned:** Limitless Labs / RK9 integration for Regionals, Internationals, and Worlds data.
