# Indigo Circuit — Project Context

**Live:** https://indigocircuit.app  
**Repo:** github.com/haidmoham/ptcg-scouting  
**Deployed on:** Railway (service: `indigo-circuit`, volume: `indigo-circuit-volume` at `/data`)

---

## What it is

Competitive Pokémon TCG intelligence platform. Two data sources:

- **Online** — Limitless TCG online tournament results (play.limitlesstcg.com/api). Ingested by `ingest/load.py`. Full match results + decklists.
- **Majors** — Official Play Pokémon events (Regionals, ICs, Worlds) scraped from Limitless Labs (labs.limitlesstcg.com). Ingested by `ingest/labs.py`. Standings + per-player match pages + decklists (top-N or full field).

---

## Stack

| Layer | Tech |
|---|---|
| Storage | DuckDB at `/data/ptcg.duckdb` (Railway persistent volume) |
| Transform | dbt (DuckDB adapter), schema: `dbt_dev_marts` |
| Backend | Flask + gunicorn (2 workers), Python 3 |
| Frontend | Vanilla JS + custom CSS (dark theme, no framework) |
| Deploy | Railway — auto-deploy on push to `main` |

**Key env vars on Railway:**
```
DUCKDB_PATH=/data/ptcg.duckdb
DBT_MARTS_SCHEMA=dbt_dev_marts
ADMIN_SECRET=<secret>
LIMITLESS_API_KEY=<key>
```

---

## Directory structure

```
ingest/
  load.py          Online tournament ingest (Limitless API → raw.*)
  labs.py          Majors ingest (Labs scraper → raw.major_*)
  glicko.py        Glicko-2 rating computation
  pipeline.sh      Runs dbt (--dbt-only flag used by scheduler)

dashboard/
  app.py           Flask app — all routes + nightly pipeline scheduler
  ev.py            Bayesian EV calculator (EV Lab feature)
  templates/
    base.html      Nav + shared CSS vars
    index.html     The League (champion/Elite 4 rankings)
    leaderboard.html  Rankings
    online.html    Online Rankings
    meta.html      Meta breakdown
    tech.html      Tech Scouting (card inclusion rates + EV per card)
    ev_lab.html    EV Lab (paste decklist → Bayesian meta EV)
    player.html    Player profile
    demons.html    Limitless Demons list
    league.html    League page

transform/
  models/
    staging/       Views over raw tables (stg_*)
    marts/         Analytics tables (materialized)
      player_tournament_history.sql
      players.sql
      player_archetype_history.sql
      player_vs_player.sql
      gym_leaders.sql
      card_archetype_stats.sql       Online card inclusion rates
      major_card_archetype_stats.sql Majors card inclusion rates
      card_match_splits.sql          Per-round splits for Bayesian EV
      seasonal_rankings.sql
```

---

## Raw tables

### Online (from load.py)
```
raw.tournaments    — tournament metadata
raw.standings      — player_username, deck_id, placing, wins/losses/ties
raw.matches        — player1, player2, winner, round (per-round pairings)
raw.decklists      — card_name, card_category, card_count per player per tournament
```

### Majors (from labs.py)
```
raw.major_tournaments  — auto-discovered from Labs index; seeded by data/major_tournaments.json (DB is source of truth)
raw.major_standings    — same shape as raw.standings
raw.major_matches      — result is 'Win'/'Loss'/'Tie' (NOT 'W'/'L'); opponent_deck_id is ALWAYS null — resolve the opponent's deck by joining opponent_name -> standings.player_name -> deck_id
raw.major_decklists    — same shape as raw.decklists
```

---

## Key dbt marts

| Model | Description |
|---|---|
| `player_tournament_history` | One row per player per tournament. Foundation model. |
| `players` | Career stats per player — consistency score, total tournaments, best placement. |
| `player_archetype_history` | Deck affinity per player — usage rate + WR per archetype. |
| `player_vs_player` | Head-to-head records between all player pairs. |
| `gym_leaders` | Top-10 specialists per archetype ranked by season-weighted placement. |
| `card_archetype_stats` | Online card inclusion rates — powers Tech Scouting. `is_tech_card` = inclusion < 30%. |
| `major_card_archetype_stats` | Same as above but from majors data. |
| `card_match_splits` | Per-round w_with/l_with/w_without/l_without by (deck_id, card_name, opponent_deck_id). Powers EV Lab. |

---

## Routes

| Route | Description |
|---|---|
| `GET /` | The League — Champion + Elite 4 |
| `GET /leaderboard` | Full seasonal rankings |
| `GET /online` | Online-only rankings |
| `GET /meta` | Meta share breakdown |
| `GET /tech` | Tech Scouting — card inclusion rates with EV popup |
| `GET /ev-lab` | EV Lab — paste decklist, get Bayesian meta EV |
| `GET /player/<username>` | Player profile |
| `GET /search` | Player search |
| `GET /demons` | Limitless Demons list |
| `GET /api/tech/decks?source=online\|majors\|both` | Deck list with list counts |
| `GET /api/tech/<deck_id>?source=...` | Card inclusion rates for a deck |
| `GET /api/tech/<deck_id>/card-ev?source=...` | Per-card EV vs each opponent archetype |
| `POST /api/ev/compute` | Bayesian list EV — body: `{decklist, archetype_id?}` |
| `GET /api/ev/archetypes` | Available archetypes for manual override |
| `POST /admin/run-pipeline` | Trigger nightly pipeline (X-Admin-Secret header) |
| `POST /admin/run-majors-scrape` | Trigger full-field majors decklist scrape |
| `GET /admin/pipeline-health?secret=` | Last validation-gate decision (pass/reject + why) |
| `GET /admin/db-holders` | Debug — show processes holding DuckDB fds |

---

## Pipeline

Nightly pipeline runs at 06:00 UTC inside the gunicorn process (scheduler thread in app.py):

1. `python3 ingest/labs.py --discover --with-decklists --current-rotation` — auto-discover new majors from the Labs index, then scrape standings + top-64 decklists for post-April-1 events (incremental; cached players skipped)
2. `python3 ingest/load.py` — refresh online tournaments
3. `python3 ingest/glicko.py` — recompute Glicko-2 ratings
4. `bash ingest/pipeline.sh --dbt-only` — rebuild all dbt marts

**Auto-discovery (no manual JSON edits):** `discover_tournaments()` scrapes the Labs landing page and parses id/name/date/tier/location/season for every listed event. `--discover` adds any not-yet-known event to `raw.major_tournaments`. The DB — not the JSON — is the scrape work-list, so discovered events survive deploys. New regionals are picked up within a day of results posting.

**Rotation:** `--current-rotation` and the `major_*` dbt marts both filter to `tournament_date >= _rotation_cutoff()` (most recent April 1). Each April this self-advances: rotated-out archetypes drop from the marts automatically, and the meta re-forms as new post-cutoff events are scraped. No code change needed at rotation.

**Validation gate (maker/checker).** Between the dbt build and the atomic swap, `dashboard/pipeline_gate.py::validate_shadow()` opens the freshly-built shadow read-only and asserts invariants: marts schema present; online marts above absolute floors AND not regressed >50% vs live; majors marts not missing (catalog/build failure) and non-empty *unless* there are genuinely no current-rotation decklists yet (legit early-April state); and no rotated archetype leaked into `major_card_archetype_stats`. Any failure → shadow discarded, last-known-good live DB kept, decision written to `/data/last_gate.json` (see `GET /admin/pipeline-health`). Fail-closed: a gate error counts as rejection. Adversarially tested by `scripts/test_pipeline_gate.py` (good + 6 broken fixtures). This is what catches the failure modes that previously went live silently (empty marts from the dbt catalog bug, rotated archetypes, a scrape that returned nothing).

DuckDB write lock management: signal file at `/data/pipeline_writing` blocks gunicorn reads during writes. Labs.py uses per-tournament connections so the lock is held only during brief write bursts (~seconds), not the full run duration.

**Majors full-field backfill** (separate, run manually or via admin endpoint):  
`POST /admin/run-majors-scrape` — runs `labs.py --discover --with-decklists --all-players` (8h timeout) followed by dbt. Use to scrape the full field (not just top-64); incremental, so already-scraped players are skipped.

---

## EV Lab (dashboard/ev.py)

Bayesian meta-weighted EV calculator for submitted decklists.

**Math:**
- Win rate modelled as `Beta(α, β)` — conjugate prior for Bernoulli
- Prior: empirical Bayes, calibrated per archetype-matchup from aggregate historical WR. Falls back to `Beta(15, 15)` (30 phantom matches at 50%)
- `card_ev_delta = E[WR|with_card] - E[WR|without_card]` — shrinks toward 0 with sparse data automatically
- `list_ev = archetype_baseline_WR + Σ tech_card_deltas` (CORE cards contribute zero by definition)
- Variance propagated end-to-end → 90% credible intervals displayed

**Data source:** `dbt_dev_marts.card_match_splits` — built from `raw.matches` + `raw.standings` + `raw.decklists` (online data). Majors match data (`raw.major_matches`) not yet wired in — will deepen signal once full-field scrape completes.

**Archetype detection:** overlap of submitted pokemon cards against CORE cards (inclusion ≥ 75%) per archetype. Returns best match above 40% overlap threshold.

---

## Pending / known gaps

- `raw.major_matches.opponent_deck_id` is resolved by joining opponent_name to major_standings — works well in practice but can miss in cases of duplicate names within a tournament
- Majors full-field scrape still running on Railway (kicked off 2026-05-26) — `major_card_archetype_stats` and majors EV data will populate once complete
- `card_match_splits` uses online data only — add majors match data path once `raw.major_matches` is fully populated
- EV Lab archetype detection confidence drops for rogue/hybrid lists — manual override dropdown available
- `ingest/pipeline.sh --dbt-only` runs all dbt models; could be scoped to only changed models for speed
- `parse_ptcglive()` and `_parse_card_text()` in labs.py share logic — candidate for refactor into `ingest/cards.py`

---

## Local dev

```bash
# Install deps
pip install -r requirements.txt
cd transform && dbt deps

# Run ingest (writes to data/ptcg.duckdb)
python3 ingest/load.py
python3 ingest/labs.py

# Build dbt marts
cd transform && dbt run

# Run dashboard
python3 -m dashboard.app   # http://localhost:5001

# Trigger EV mart specifically
cd transform && dbt run --select card_match_splits
```

**DuckDB path:** `data/ptcg.duckdb` locally, `/data/ptcg.duckdb` on Railway (volume mount).  
**Admin secret:** stored in Railway env vars. Locally: `export ADMIN_SECRET=$(railway variables --service indigo-circuit --json | python3 -c "import sys,json; print(json.load(sys.stdin)['ADMIN_SECRET'])")`
