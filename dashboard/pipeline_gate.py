"""
Pipeline validation gate — the *checker* in the maker/checker split.

The nightly pipeline (the *maker*) builds a shadow DB and atomically swaps it to
live. This module runs in between: it opens the freshly-built shadow read-only
and asserts a set of invariants. If any fail, the caller discards the shadow and
keeps the last-known-good live DB. That turns silent data corruption — empty
marts from a dbt/catalog failure, rotated archetypes leaking through, a scrape
that returned mostly nothing — into a logged rejection that never reaches users.

Pure and importable: no Flask, no scheduler. Exercised by
scripts/test_pipeline_gate.py against both good and deliberately-broken fixtures.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import duckdb


def rotation_cutoff() -> str:
    """ISO date of the most recent April 1 (current Standard rotation start).
    Mirrors dashboard/app.py and ingest/labs.py so all three agree."""
    today = date.today()
    year = today.year if (today.month, today.day) >= (4, 1) else today.year - 1
    return f"{year}-04-01"


# Online marts reflect a rolling multi-year window — they never legitimately
# crater, so they get an absolute floor AND a regression check against live.
_ONLINE_FLOORS = {
    "card_archetype_stats": 100,
    "card_match_splits":     100,
    "players":               100,
    "seasonal_rankings":      50,
}

# Majors marts legitimately shrink toward zero right after an April rotation
# ("wait for the new meta to formulate"), so they get NO regression check —
# only a must-not-be-missing check, plus a non-empty check that is conditional
# on current-rotation decklists actually existing (see below).
_MAJORS_MARTS = ("major_card_archetype_stats", "major_card_match_splits")

# An online mart dropping below this fraction of its live row count signals a
# broken scrape/load or a partial dbt build.
_REGRESSION_FLOOR = 0.5


def _count(conn, schema: str, table: str):
    """Row count, or None if the table is missing (catches a dropped/unbuilt mart)."""
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()[0]
    except Exception:
        return None


def validate_shadow(shadow_path: str, live_path: str | None = None,
                    marts_schema: str = "dbt_dev_marts",
                    cutoff: str | None = None) -> tuple[bool, list[str], dict]:
    """Validate a freshly-built shadow DB before it is swapped to live.

    Returns (ok, failures, metrics). `ok` is True only when every invariant
    holds. Fail-closed: any internal error becomes a failure — if we cannot
    prove the data is good, we do not publish it.
    """
    failures: list[str] = []
    metrics: dict = {}
    cutoff = cutoff or rotation_cutoff()
    metrics["cutoff"] = cutoff

    if not os.path.exists(shadow_path):
        return False, [f"shadow DB does not exist: {shadow_path}"], metrics

    try:
        conn = duckdb.connect(shadow_path, read_only=True)
    except Exception as e:
        return False, [f"cannot open shadow DB: {e}"], metrics

    try:
        # 1. Marts schema present — proves dbt ran at all.
        schemas = {r[0] for r in conn.execute(
            "SELECT schema_name FROM information_schema.schemata").fetchall()}
        if marts_schema not in schemas:
            failures.append(f"marts schema '{marts_schema}' missing — dbt did not build")
            return False, failures, metrics  # nothing else is checkable

        # 2. Online marts present and above an absolute floor.
        online = {}
        for tbl, floor in _ONLINE_FLOORS.items():
            n = _count(conn, marts_schema, tbl)
            online[tbl] = n
            if n is None:
                failures.append(f"online mart '{tbl}' missing")
            elif n < floor:
                failures.append(f"online mart '{tbl}' has {n} rows (floor {floor})")
        metrics["online"] = online

        # 3. Majors marts must not be MISSING (catalog/build failure). They may
        #    be EMPTY only if there are genuinely no current-rotation decklists
        #    yet — the legitimate early-April "waiting for the meta" state.
        try:
            post_cutoff_decklists = conn.execute("""
                SELECT COUNT(*) FROM raw.major_decklists d
                JOIN raw.major_tournaments mt ON d.labs_tournament_id = mt.labs_id
                WHERE mt.tournament_date >= ?
            """, (cutoff,)).fetchone()[0]
        except Exception:
            post_cutoff_decklists = None
        metrics["post_cutoff_decklists"] = post_cutoff_decklists

        majors = {}
        for tbl in _MAJORS_MARTS:
            n = _count(conn, marts_schema, tbl)
            majors[tbl] = n
            if n is None:
                failures.append(f"majors mart '{tbl}' missing — likely dbt/catalog failure")
            elif n == 0 and (post_cutoff_decklists or 0) > 0:
                failures.append(
                    f"majors mart '{tbl}' is empty despite "
                    f"{post_cutoff_decklists} current-rotation decklists")
        metrics["majors"] = majors

        # 4. Rotation invariant — every archetype in major_card_archetype_stats
        #    must appear in at least one current-rotation tournament. Proves the
        #    dbt rotation filter held; catches rotated-archetype leakage.
        if majors.get("major_card_archetype_stats"):
            try:
                leaked = conn.execute(f"""
                    SELECT COUNT(*) FROM (
                        SELECT DISTINCT mca.deck_id
                        FROM "{marts_schema}".major_card_archetype_stats mca
                        WHERE mca.deck_id NOT IN (
                            SELECT DISTINCT ms.deck_id
                            FROM raw.major_standings ms
                            JOIN raw.major_tournaments mt
                              ON ms.labs_tournament_id = mt.labs_id
                            WHERE mt.tournament_date >= ?
                        )
                    )
                """, (cutoff,)).fetchone()[0]
                metrics["rotated_archetypes"] = leaked
                if leaked > 0:
                    failures.append(
                        f"{leaked} archetype(s) in major_card_archetype_stats map "
                        f"only to pre-{cutoff} (rotated) tournaments")
            except Exception as e:
                failures.append(f"rotation check errored: {e}")

        # 5. Regression vs live — online marts must not crater. Skipped on first
        #    boot (no live yet) and for majors marts (they rotate by design).
        if live_path and os.path.exists(live_path):
            try:
                lc = duckdb.connect(live_path, read_only=True)
                try:
                    reg = {}
                    for tbl in ("card_archetype_stats", "players"):
                        s = online.get(tbl)
                        l = _count(lc, marts_schema, tbl)
                        reg[tbl] = {"shadow": s, "live": l}
                        if s is not None and l and s < l * _REGRESSION_FLOOR:
                            failures.append(
                                f"online mart '{tbl}' regressed: {s} rows vs {l} "
                                f"live (< {int(_REGRESSION_FLOOR * 100)}%)")
                    metrics["regression"] = reg
                finally:
                    lc.close()
            except Exception as e:
                metrics["regression_error"] = str(e)  # don't block on unreadable live
    except Exception as e:
        failures.append(f"gate errored: {e}")
    finally:
        conn.close()

    return (len(failures) == 0), failures, metrics


def result_record(ok: bool, failures: list[str], metrics: dict, action: str) -> dict:
    """Serializable record of a gate decision, for the health endpoint / logs."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": ok,
        "action": action,           # "swapped" | "discarded"
        "failures": failures,
        "metrics": metrics,
    }
