"""Read-only checks for the data snapshot served by the dashboard."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from .pipeline_gate import validate_shadow


_RAW_TABLES = (
    "tournaments", "standings", "matches", "decklists", "glicko_ratings",
    "major_tournaments", "major_standings", "major_matches", "major_decklists",
)
_MART_TABLES = (
    "players", "players_enriched", "player_archetype_history", "player_vs_player",
    "seasonal_rankings", "major_player_history", "major_gym_leaders",
    "card_archetype_stats", "card_match_splits", "card_copy_splits",
    "major_card_archetype_stats", "major_card_match_splits", "major_card_copy_splits",
)
_SOURCES = {
    "online": """
        SELECT count(DISTINCT s.tournament_id), count(*),
               max(t.tournament_date), max(s._loaded_at)
        FROM raw.standings s
        JOIN raw.tournaments t ON s.tournament_id = t.id
    """,
    "majors": """
        SELECT count(DISTINCT s.labs_tournament_id), count(*),
               max(t.tournament_date), max(s._loaded_at)
        FROM raw.major_standings s
        JOIN raw.major_tournaments t ON s.labs_tournament_id = t.labs_id
    """,
}


def _utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


def snapshot_health(database_path, marts_schema="dbt_dev_marts", max_age_hours=72):
    """Separate a missing/broken snapshot from available but old result data.

    The timestamps describe stored results. They do not establish that a source
    was successfully checked today or that every published event was ingested.
    """
    now = datetime.now(timezone.utc)
    result = {
        "status": "missing",
        "available": False,
        "ready": False,
        "checked_at": now.isoformat(timespec="seconds"),
        "max_data_age_hours": max_age_hours,
        "freshness_basis": "newest stored result load time; source coverage is not guaranteed",
        "sources": {},
    }
    if not Path(database_path).is_file():
        result["reason"] = "database_missing"
        return result

    try:
        conn = duckdb.connect(str(database_path), read_only=True)
        try:
            tables = {
                (schema.lower(), table.lower())
                for schema, table in conn.execute(
                    "SELECT table_schema, table_name FROM information_schema.tables"
                ).fetchall()
            }
            required = [("raw", table) for table in _RAW_TABLES]
            required += [(marts_schema, table) for table in _MART_TABLES]
            missing = [f"{schema}.{table}" for schema, table in required
                       if (schema.lower(), table.lower()) not in tables]
            if missing:
                result.update(status="incomplete", reason="required_tables_missing", missing_tables=missing)
                return result

            for name, sql in _SOURCES.items():
                events, rows, latest_event, latest_load = conn.execute(sql).fetchone()
                latest_load = _utc(latest_load)
                age = (now - latest_load).total_seconds() / 3600 if latest_load else None
                result["sources"][name] = {
                    "events": events,
                    "results": rows,
                    "latest_event_date": latest_event.isoformat() if latest_event else None,
                    "latest_result_loaded_at": latest_load.isoformat(timespec="seconds") if latest_load else None,
                    "age_hours": round(age, 1) if age is not None else None,
                    "stale": age is None or age > max_age_hours,
                }
        finally:
            conn.close()

        ok, failures, _ = validate_shadow(str(database_path), marts_schema=marts_schema)
        result["gate"] = {"ok": ok, "failures": failures}
        if not ok:
            result.update(status="incomplete", reason="pipeline_gate_rejected")
            return result
        if any(source["results"] == 0 for source in result["sources"].values()):
            result.update(status="incomplete", reason="source_results_missing")
            return result
        if any(_utc(datetime.fromisoformat(source["latest_result_loaded_at"])) > now + timedelta(minutes=5)
               for source in result["sources"].values() if source["latest_result_loaded_at"]):
            result.update(status="error", reason="source_timestamp_in_future")
            return result

        stale = any(source["stale"] for source in result["sources"].values())
        result.update(status="stale" if stale else "ok", available=True, ready=not stale)
        return result
    except Exception as exc:
        # Public health must not disclose filesystem paths, SQL, or source data
        # in exception messages. The exception class still distinguishes errors.
        result.update(status="error", reason="database_check_failed", error_type=type(exc).__name__)
        return result
