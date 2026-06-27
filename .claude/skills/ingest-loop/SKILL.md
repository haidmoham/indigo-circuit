---
name: ingest-loop
description: >-
  Run, gate, and (if needed) retry the Indigo Circuit nightly data pipeline
  (Limitless Labs + API ingest -> Glicko -> dbt marts). Use when asked to run
  the ingest, refresh the marts, recover a failed/missed nightly run, or verify
  pipeline output. The gate is dashboard/pipeline_gate.validate_shadow via
  scripts/verify_ingest.py; the pipeline is ingest/pipeline.sh. Reusable loop
  pattern — the same manual-run -> skill -> gate+stop -> schedule shape applies
  to the job-search-sync pipeline (run_sync.sh) and any other recurring job.
---

# Indigo Circuit — ingest loop

A **gated retry loop** around the nightly data pipeline. Built in the order that
survives in production (per loop-engineering practice): prove one manual run,
save it as this skill, wrap it in a gate + stop condition, *then* let it run on a
schedule. Steps 1–3 are this skill; step 4 already exists (Railway cron).

## The loop, in one spec

```text
GOAL:    a freshly-built DuckDB whose marts clear every invariant in
         dashboard/pipeline_gate.validate_shadow.

EACH ITERATION:
  1. EXECUTE  bash ingest/pipeline.sh          (or --dbt-only to skip scraping)
  2. VERIFY   python3 scripts/verify_ingest.py (exit 0 = pass, 1 = fail)
  3. DECIDE   pass -> STOP (success). fail -> read failures, fix the narrowest
              cause, ITERATE.

STOP WHEN:  verify passes, OR 3 iterations reached.
ON STOP:    print the final VERIFY_JSON line and a one-paragraph summary of what
            ran, what changed, and (on failure) the exact failing invariants.
```

The hard cap (3) is the Ralph-Wiggum guard: ingest scraping is slow and
token/network-expensive, so the loop must never spin all night re-confirming the
same failure. If it is still red after 3 passes, the cause is structural (source
site down, schema drift, dbt model broken) and needs a human, not another run.

## Step-by-step

### 1. EXECUTE — run the pipeline

```bash
cd /Users/haidmoham/projects/ptcg-scouting
# Full nightly run (scrape Labs + API, recompute Glicko, rebuild marts):
bash ingest/pipeline.sh
# Marts-only (fast; use when raw data is fine and only dbt changed):
bash ingest/pipeline.sh --dbt-only
```

Set `DUCKDB_PATH` first if not using the deploy default (`/data/ptcg.duckdb`).
Locally that is usually `data/ptcg.duckdb`:

```bash
export DUCKDB_PATH="$PWD/data/ptcg.duckdb"
```

`ingest/load.py` and `ingest/labs.py` are **idempotent** (`ON CONFLICT DO
NOTHING`, already-scraped players are skipped), so a re-run resumes rather than
duplicating. `ingest/glicko.py` recomputes from scratch (also safe). That
idempotency is what makes the loop's retries cheap and correct.

### 2. VERIFY — the gate

```bash
python3 scripts/verify_ingest.py            # validates $DUCKDB_PATH, exit 0/1
python3 scripts/verify_ingest.py --with-dbt-test   # also run dbt test (soft)
```

This calls the **same** `validate_shadow` the production scheduler trusts, so the
loop is graded by exactly the production rules. It prints one parseable line:

```text
VERIFY_JSON: {"ok": true, "failures": [], "metrics": {"online": {...}, ...}}
```

Invariants enforced (see `dashboard/pipeline_gate.py`): marts schema exists;
online marts above absolute floors (`players`≥100, etc.); majors marts present
and non-empty *unless* no post-rotation decklists exist yet; no pre-rotation
archetype leaks into current-season marts; online marts ≥50% of last-known-good
row counts. Fail-closed — any internal error counts as a failure.

### 3. DECIDE — read failures, fix the narrowest cause, iterate

Map the failing invariant to the narrowest fix, then loop:

| Failure substring | Likely cause | Narrowest fix before re-running |
| --- | --- | --- |
| `marts schema ... missing` / empty marts | dbt didn't run / catalog error | `bash ingest/pipeline.sh --dbt-only`, read dbt output |
| online mart below floor (`players` etc.) | scrape returned little | check `ingest/load.py` output; `LIMITLESS_API_KEY` set? |
| `< 50% of live` (regression) | partial scrape/load | re-run full pipeline; check source site availability |
| majors empty but decklists exist | incomplete dbt build of majors marts | `--dbt-only`; inspect `transform/models/marts/major_*` |
| rotated archetype leak | rotation filter bug | inspect `rotation_cutoff()` agreement across labs/gate/app |

### 4. SCHEDULE — already wired (do not rebuild)

Two schedulers fire at **06:00 UTC**: the Railway cron (`railway-pipeline.toml`
-> `bash ingest/pipeline.sh`) and the dashboard daemon thread (`dashboard/app.py`
`_start_scheduler`), which is the **gated** path (shadow build -> validate_shadow
-> atomic swap; keeps last-known-good on failure). Health is queryable at
`GET /admin/pipeline-health?secret=$ADMIN_SECRET`, which returns the last
`gate_result.json`.

This skill is for **on-demand runs, recovering a missed/failed night, and
`/loop`-driven retries** — not for replacing the cron.

## Driving it with /loop

```text
/loop /ingest-loop run the Indigo Circuit ingest, verify, retry up to 3x, then report
```

Omit an interval to let the model self-pace the retry loop; the stop condition
above bounds it. For unattended scheduled recovery use the existing cron, not
`/loop`.

## Known gaps (the honest part)

- **dbt tests are defined but not yet a hard gate.** `transform/models/**/*.yml`
  declare source/model tests that the pipeline never runs. `verify_ingest.py
  --with-dbt-test` runs them as a *soft* signal. Promote to a hard gate
  (`--with-dbt-test --strict-dbt` in the scheduler) only after they pass on real
  data for a week — don't trust an unproven gate.
- **No proactive alert on gate rejection.** A rejected swap only logs + updates
  `gate_result.json`; nobody is pinged. That's a silent-failure (Ralph-Wiggum)
  risk for the *scheduled* path. A small connector (email/Telegram on
  `ok == false`) would close it.
- **The Railway-cron path is ungated.** `ingest/pipeline.sh` alone builds marts
  without `validate_shadow`/shadow-swap — only the dashboard scheduler path is
  gated. If both fire, prefer consolidating on the gated path.

## Reusing this pattern (job-search-sync, etc.)

The shape — **one reliable manual run -> save as a skill -> wrap in a verifiable
gate + hard stop -> only then schedule** — is project-agnostic. It already exists
in a sibling repo: `~/projects/job-search-sync/run_sync.sh` is the same pattern
in shell (per-stage exit-code gates, `lpass` pre-flight gate, `STATE.md` audit
log, `SUMMARY_JSON:` lines). When building a loop for any recurring job, copy the
*structure*, not the code:

1. A single command that does one stage reliably.
2. An external, automatic pass/fail check (test, row-count floor, type-check) —
   never let the maker grade its own work.
3. A hard iteration cap + a state/audit record so a re-run resumes and reports.
4. A schedule, added last, only after 1–3 are proven by hand.
