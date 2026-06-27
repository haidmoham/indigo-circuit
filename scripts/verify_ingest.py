#!/usr/bin/env python3
"""
verify_ingest.py — the *gate* for the ingest loop.

This is the "VERIFY" step the loop checks. It re-uses the same invariant set the
nightly scheduler already trusts (dashboard/pipeline_gate.validate_shadow), so a
manual/loop-driven run is graded by exactly the same rules as production — no
second, divergent definition of "good data".

Exit code IS the gate:
    0  -> every invariant holds. Data is publishable. Loop may STOP (success).
    1  -> at least one invariant failed. Loop should ITERATE or report.

It always prints one machine-readable line to stdout so a loop / cron / log
scrape can parse it without re-running anything:

    VERIFY_JSON: {"ok": false, "failures": [...], "metrics": {...}}

Usage:
    python3 scripts/verify_ingest.py                 # validate the live DB ($DUCKDB_PATH)
    python3 scripts/verify_ingest.py --db data/ptcg.duckdb
    python3 scripts/verify_ingest.py --with-dbt-test # also run `dbt test` (soft signal)
    python3 scripts/verify_ingest.py --with-dbt-test --strict-dbt  # make dbt test a hard gate

Why dbt test is OFF by default: the dbt source/model tests in transform/models/
are *defined* but have never been run against real data in the pipeline. Per the
build order, you do not promote an unproven check to a hard gate. Run it with
--with-dbt-test first, watch it go green for a week, THEN add --strict-dbt to the
scheduled invocation. Until then, validate_shadow (proven + adversarially tested
in scripts/test_pipeline_gate.py) is the real gate.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dashboard.pipeline_gate import validate_shadow  # noqa: E402


def run_dbt_test(duckdb_path: str) -> tuple[bool, str]:
    """Generate the dbt profile, run `dbt test`, return (passed, tail_of_output)."""
    transform = REPO / "transform"
    profiles = transform / "profiles.yml"
    profiles.write_text(
        "ptcg_scouting:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        f'      path: "{duckdb_path}"\n'
        "      schema: dbt_dev\n"
        "      threads: 4\n"
    )
    env = {**os.environ, "PATH": f"/opt/venv/bin:{os.environ.get('PATH', '')}"}
    try:
        proc = subprocess.run(
            ["dbt", "test", "--profiles-dir", ".", "--target", "dev"],
            cwd=transform, env=env, capture_output=True, text=True, timeout=900,
        )
        tail = (proc.stdout + proc.stderr)[-1500:]
        return proc.returncode == 0, tail
    except FileNotFoundError:
        return False, "dbt not found on PATH (skip --with-dbt-test outside the deploy image)"
    except subprocess.TimeoutExpired:
        return False, "dbt test timed out after 900s"
    finally:
        profiles.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate the ingest output before trusting it.")
    ap.add_argument("--db", default=os.environ.get("DUCKDB_PATH", "/data/ptcg.duckdb"),
                    help="Path to the DuckDB file to validate (default: $DUCKDB_PATH).")
    ap.add_argument("--with-dbt-test", action="store_true",
                    help="Also run `dbt test` (reported; soft unless --strict-dbt).")
    ap.add_argument("--strict-dbt", action="store_true",
                    help="Make a `dbt test` failure fail the gate too.")
    args = ap.parse_args()

    ok, failures, metrics = validate_shadow(args.db, live_path=None)

    dbt_ok = None
    if args.with_dbt_test:
        dbt_ok, dbt_tail = run_dbt_test(args.db)
        metrics["dbt_test_passed"] = dbt_ok
        if not dbt_ok:
            print(f"[verify] dbt test output (tail):\n{dbt_tail}", file=sys.stderr)
            if args.strict_dbt:
                ok = False
                failures = [*failures, "dbt test failed (--strict-dbt)"]

    print(f"VERIFY_JSON: {json.dumps({'ok': ok, 'failures': failures, 'metrics': metrics})}")

    if ok:
        print(f"[verify] PASS — data at {args.db} clears every invariant.", file=sys.stderr)
    else:
        print(f"[verify] FAIL — {len(failures)} invariant(s) broken: {failures}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
