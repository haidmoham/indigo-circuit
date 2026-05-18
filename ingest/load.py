"""
Ingest Limitless TCG tournament data into DuckDB RAW schema.

Usage:
    python ingest/load.py                  # load all PTCG tournaments
    python ingest/load.py --tournament <id> # reload a single tournament
    python ingest/load.py --dry-run        # fetch only, no DB writes
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import duckdb
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from client import LimitlessClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DUCKDB_PATH = os.environ.get("DUCKDB_PATH", str(Path(__file__).parent.parent / "data" / "ptcg.duckdb"))

DDL = {
    "TOURNAMENTS": """
        CREATE TABLE IF NOT EXISTS raw.tournaments (
            id              VARCHAR  PRIMARY KEY,
            game            VARCHAR,
            format          VARCHAR,
            name            VARCHAR,
            tournament_date TIMESTAMPTZ,
            player_count    INTEGER,
            _loaded_at      TIMESTAMPTZ DEFAULT now()
        )
    """,
    "STANDINGS": """
        CREATE TABLE IF NOT EXISTS raw.standings (
            tournament_id   VARCHAR  NOT NULL,
            player_username VARCHAR  NOT NULL,
            player_name     VARCHAR,
            country         VARCHAR,
            placing         INTEGER,
            wins            INTEGER,
            losses          INTEGER,
            ties            INTEGER,
            deck_id         VARCHAR,
            deck_name       VARCHAR,
            deck_icons      VARCHAR,
            drop_round      INTEGER,
            _loaded_at      TIMESTAMPTZ DEFAULT now()
        )
    """,
    "MATCHES": """
        CREATE TABLE IF NOT EXISTS raw.matches (
            tournament_id VARCHAR  NOT NULL,
            round         INTEGER,
            phase         INTEGER,
            table_number  INTEGER,
            match_id      VARCHAR,
            player1       VARCHAR,
            player2       VARCHAR,
            winner        VARCHAR,
            _loaded_at    TIMESTAMPTZ DEFAULT now()
        )
    """,
    "DECKLISTS": """
        CREATE TABLE IF NOT EXISTS raw.decklists (
            tournament_id   VARCHAR  NOT NULL,
            player_username VARCHAR  NOT NULL,
            card_category   VARCHAR,
            card_name       VARCHAR,
            card_set        VARCHAR,
            card_number     VARCHAR,
            card_count      INTEGER,
            _loaded_at      TIMESTAMPTZ DEFAULT now()
        )
    """,
}


def get_conn():
    return duckdb.connect(DUCKDB_PATH)


def ensure_schema(conn):
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    for ddl in DDL.values():
        conn.execute(ddl)
    log.info("Schema ready")


def load_tournaments(conn, client: LimitlessClient, max_pages: int = 3) -> int:
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=3 * 365)
    total = 0
    for page in range(1, max_pages + 1):
        rows = client.get_tournaments(limit=100, page=page)
        if not rows:
            break
        page_rows = []
        hit_cutoff = False
        for t in rows:
            date_str = t.get("date", "")
            if date_str:
                t_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if t_date < cutoff:
                    hit_cutoff = True
                    break
            page_rows.append(t)

        for t in page_rows:
            conn.execute(
                """
                INSERT INTO raw.tournaments (id, game, format, name, tournament_date, player_count)
                VALUES (?, ?, ?, ?, ?::TIMESTAMPTZ, ?)
                ON CONFLICT (id) DO NOTHING
                """,
                (t["id"], t.get("game"), t.get("format"), t.get("name"), t.get("date"), t.get("players")),
            )
        total += len(page_rows)
        log.info(f"  page {page}: {len(page_rows)} tournaments ({total} total)")
        if hit_cutoff or len(rows) < 100:
            break
    return total


def load_tournament_data(conn, client: LimitlessClient, tournament_id: str):
    standings = client.get_standings(tournament_id)
    conn.executemany(
        """
        INSERT INTO raw.standings
          (tournament_id, player_username, player_name, country,
           placing, wins, losses, ties,
           deck_id, deck_name, deck_icons, drop_round)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                tournament_id,
                s.get("player"),
                s.get("name"),
                s.get("country"),
                s.get("placing"),
                (s.get("record") or {}).get("wins"),
                (s.get("record") or {}).get("losses"),
                (s.get("record") or {}).get("ties"),
                (s.get("deck") or {}).get("id"),
                (s.get("deck") or {}).get("name"),
                json.dumps((s.get("deck") or {}).get("icons", [])),
                s.get("drop"),
            )
            for s in standings
        ],
    )

    # deck lists
    decklist_rows = []
    for s in standings:
        username = s.get("player")
        decklist = s.get("decklist") or {}
        for category in ("pokemon", "trainer", "energy"):
            for card in decklist.get(category, []):
                decklist_rows.append((
                    tournament_id, username, category,
                    card.get("name"), card.get("set"),
                    card.get("number"), card.get("count"),
                ))
    if decklist_rows:
        conn.executemany(
            """
            INSERT INTO raw.decklists
              (tournament_id, player_username, card_category,
               card_name, card_set, card_number, card_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            decklist_rows,
        )

    pairings = client.get_pairings(tournament_id)
    if pairings:
        conn.executemany(
            """
            INSERT INTO raw.matches
              (tournament_id, round, phase, table_number, match_id, player1, player2, winner)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    tournament_id,
                    p.get("round"), p.get("phase"), p.get("table"),
                    p.get("match"), p.get("player1"), p.get("player2"),
                    str(p.get("winner")) if p.get("winner") is not None else None,
                )
                for p in pairings
            ],
        )

    log.info(f"  {tournament_id}: {len(standings)} standings, {len(pairings)} pairings")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tournament", help="Load a single tournament by ID")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, no DB writes")
    parser.add_argument("--pages", type=int, default=3, help="Max pages to scan (default 3). Use --pages 999 for full historical load.")
    args = parser.parse_args()

    client = LimitlessClient(api_key=os.environ.get("LIMITLESS_API_KEY"))

    if args.dry_run:
        rows = client.get_tournaments(limit=5)
        log.info(f"Dry run: fetched {len(rows)} tournaments — first: {rows[0]}")
        return

    conn = get_conn()
    try:
        ensure_schema(conn)

        if args.tournament:
            tournament_ids = [args.tournament]
        else:
            n = load_tournaments(conn, client, max_pages=args.pages)
            log.info(f"Tournament index: {n} loaded")
            cur = conn.execute("""
                SELECT id FROM raw.tournaments
                WHERE game = 'PTCG'
                  AND player_count >= 64
                  AND tournament_date >= current_date - INTERVAL '3 years'
                ORDER BY tournament_date DESC
            """)
            tournament_ids = [r[0] for r in cur.fetchall()]

        log.info(f"Loading data for {len(tournament_ids)} tournaments...")
        for tid in tournament_ids:
            cur = conn.execute("SELECT COUNT(*) FROM raw.standings WHERE tournament_id = ?", (tid,))
            if cur.fetchone()[0] > 0 and not args.tournament:
                continue
            try:
                load_tournament_data(conn, client, tid)
            except Exception as e:
                log.warning(f"  {tid}: skipped — {e}")

        conn.commit()
        log.info("Done")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
