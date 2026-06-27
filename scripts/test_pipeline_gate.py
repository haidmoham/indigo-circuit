#!/usr/bin/env python3
"""
Adversarial test for the pipeline validation gate (dashboard/pipeline_gate.py).

Builds DuckDB fixtures that mirror the real schema and asserts the gate:
  - PASSES a healthy shadow,
  - PASSES a legitimately-empty majors view right after rotation,
  - REJECTS each failure mode we have actually hit in production.

A gate that only ever sees good data is untested branching; this is the
"who checks the checker?" step. Run:  python3 scripts/test_pipeline_gate.py
"""
import os
import sys
import tempfile

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard.pipeline_gate import validate_shadow  # noqa: E402

CUTOFF = "2026-04-01"
MARTS = "dbt_dev_marts"


def build_db(path, *, marts_schema=True,
             online=None,                       # {mart: rowcount}
             major_archetype_decks=("dragapult", "gardevoir"),  # None => skip table
             major_match_rows=50,               # None => skip table
             standings=(("dragapult", "0068"), ("gardevoir", "0068")),
             tournaments=(("0068", "2026-05-31"),),
             post_cutoff_decklists=200):
    """Create a fixture DB. Defaults describe a healthy current-rotation shadow."""
    online = online or {"card_archetype_stats": 6965, "card_match_splits": 8518,
                        "players": 2891, "seasonal_rankings": 2919}
    if os.path.exists(path):
        os.remove(path)
    c = duckdb.connect(path)

    # --- raw schema (used by the rotation + empty-majors checks) ---
    c.execute("CREATE SCHEMA raw")
    c.execute("CREATE TABLE raw.major_tournaments (labs_id VARCHAR, tournament_date DATE)")
    for lid, d in tournaments:
        c.execute("INSERT INTO raw.major_tournaments VALUES (?, ?::DATE)", (lid, d))
    c.execute("CREATE TABLE raw.major_standings (labs_tournament_id VARCHAR, deck_id VARCHAR)")
    for deck, lid in standings:
        c.execute("INSERT INTO raw.major_standings VALUES (?, ?)", (lid, deck))
    # decklists: rows attributed to the first (assumed post-cutoff) tournament
    c.execute("CREATE TABLE raw.major_decklists (labs_tournament_id VARCHAR, card_name VARCHAR)")
    if post_cutoff_decklists and tournaments:
        c.execute(
            "INSERT INTO raw.major_decklists "
            f"SELECT '{tournaments[0][0]}', 'c' || i FROM range({post_cutoff_decklists}) t(i)")

    # --- marts schema ---
    if marts_schema:
        c.execute(f'CREATE SCHEMA "{MARTS}"')
        for mart, n in online.items():
            c.execute(f'CREATE TABLE "{MARTS}"."{mart}" AS SELECT i AS k FROM range({n}) t(i)')
        if major_archetype_decks is not None:
            c.execute(f'CREATE TABLE "{MARTS}".major_card_archetype_stats (deck_id VARCHAR, total_lists INT)')
            for deck in major_archetype_decks:
                c.execute(f'INSERT INTO "{MARTS}".major_card_archetype_stats VALUES (?, 30)', (deck,))
        if major_match_rows is not None:
            c.execute(f'CREATE TABLE "{MARTS}".major_card_match_splits AS '
                      f'SELECT i AS k FROM range({major_match_rows}) t(i)')
    c.close()


def run_case(name, expect_ok, *, live=None, must_mention=None, **kw):
    d = tempfile.mkdtemp()
    shadow = os.path.join(d, "shadow.duckdb")
    build_db(shadow, **kw)
    live_path = None
    if live is not None:
        live_path = os.path.join(d, "live.duckdb")
        build_db(live_path, **live)
    ok, failures, metrics = validate_shadow(shadow, live_path, marts_schema=MARTS, cutoff=CUTOFF)
    passed = (ok == expect_ok)
    if must_mention and ok is False:
        passed = passed and any(must_mention in f for f in failures)
    status = "PASS" if passed else "XXXX FAIL"
    verdict = "accept" if ok else "REJECT"
    print(f"  [{status}] {name}: gate said {verdict}"
          + (f"  -> {failures}" if failures else ""))
    return passed


def main():
    print("Pipeline gate — adversarial test\n")
    results = []

    # --- Cases that MUST pass ---
    results.append(run_case("healthy shadow", True, live={}))
    results.append(run_case(
        "empty majors view, early rotation (no current decklists)", True,
        major_archetype_decks=(), major_match_rows=0,
        standings=(), post_cutoff_decklists=0))

    # --- Cases that MUST be rejected ---
    results.append(run_case(
        "marts schema missing (dbt didn't run)", False,
        marts_schema=False, must_mention="schema"))
    results.append(run_case(
        "majors mart missing (catalog/build failure)", False,
        major_archetype_decks=None, must_mention="missing"))
    results.append(run_case(
        "majors mart empty despite current decklists", False,
        major_archetype_decks=(), must_mention="empty"))
    results.append(run_case(
        "rotated archetype leaked into majors mart", False,
        major_archetype_decks=("dragapult", "charizard"),
        standings=(("dragapult", "0068"), ("charizard", "0027")),
        tournaments=(("0068", "2026-05-31"), ("0027", "2025-04-27")),
        must_mention="rotated"))
    results.append(run_case(
        "online mart cratered vs live (broken scrape)", False,
        online={"card_archetype_stats": 30, "card_match_splits": 8518,
                "players": 2891, "seasonal_rankings": 2919},
        live={}, must_mention="card_archetype_stats"))
    results.append(run_case(
        "online mart below absolute floor", False,
        online={"card_archetype_stats": 6965, "card_match_splits": 8518,
                "players": 5, "seasonal_rankings": 2919},
        must_mention="players"))

    n_pass = sum(results)
    print(f"\n{n_pass}/{len(results)} cases behaved as expected")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
