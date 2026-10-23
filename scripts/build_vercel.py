"""Validate a private DuckDB snapshot and prepare only static CDN assets."""

import gzip
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.health import snapshot_health  # noqa: E402


def main():
    database = ROOT / "data" / "ptcg.duckdb"
    compressed = database.with_suffix(".duckdb.gz")
    candidate = None
    temporary_directory = None
    try:
        if compressed.is_file():
            # Keep the existing candidate intact until the uploaded replacement
            # has decompressed and passed the same production checker.
            # dbt views bind the ptcg catalog. Preserve the basename while
            # validating a replacement, rather than opening it as snapshot_*.
            temporary_directory = tempfile.TemporaryDirectory(dir=database.parent, prefix="snapshot_")
            candidate = Path(temporary_directory.name) / "ptcg.duckdb"
            with candidate.open("wb") as target, gzip.open(compressed, "rb") as source:
                shutil.copyfileobj(source, target)
        health = snapshot_health(candidate or database, marts_schema=os.environ.get("DBT_MARTS_SCHEMA", "dbt_dev_marts"))
        print("SNAPSHOT_HEALTH: " + json.dumps(health))
        if not health["available"]:
            raise SystemExit("Build refused: a populated snapshot passing the production pipeline gate is required.")
        if candidate:
            candidate.replace(database)
    finally:
        if candidate:
            candidate.unlink(missing_ok=True)
        if temporary_directory:
            temporary_directory.cleanup()
    if health["status"] == "stale":
        print("WARNING: stored results are stale; /api/health will report stale and HTTP 503.")

    # Vercel serves public/** from its CDN. The DB remains outside public/ and
    # is included only in the private Python function bundle by vercel.json.
    public = ROOT / "public"
    if public.exists() and any(public.rglob("*.duckdb*")):
        raise SystemExit("Build refused: a database file was found under public/.")
    shutil.copytree(ROOT / "dashboard" / "static", public / "static", dirs_exist_ok=True)
    print("Prepared public/static; the DuckDB snapshot remains private.")


if __name__ == "__main__":
    main()
