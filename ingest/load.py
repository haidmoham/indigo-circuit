"""
Ingest Limitless TCG tournament data into Snowflake RAW schema.

Usage:
    python ingest/load.py                  # load all PTCG tournaments
    python ingest/load.py --tournament <id> # reload a single tournament
    python ingest/load.py --dry-run        # fetch only, no writes
"""
import argparse
import json
import logging
import os
import sys

import snowflake.connector
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from client import LimitlessClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DDL = {
    "TOURNAMENTS": """
        CREATE TABLE IF NOT EXISTS PTCG_SCOUTING.RAW.TOURNAMENTS (
            ID             STRING        NOT NULL,
            GAME           STRING,
            FORMAT         STRING,
            NAME           STRING,
            TOURNAMENT_DATE TIMESTAMP_TZ,
            PLAYER_COUNT   INTEGER,
            _LOADED_AT     TIMESTAMP_TZ  DEFAULT CURRENT_TIMESTAMP(),
            CONSTRAINT PK_TOURNAMENTS PRIMARY KEY (ID)
        )
    """,
    "STANDINGS": """
        CREATE TABLE IF NOT EXISTS PTCG_SCOUTING.RAW.STANDINGS (
            TOURNAMENT_ID   STRING   NOT NULL,
            PLAYER_USERNAME STRING   NOT NULL,
            PLAYER_NAME     STRING,
            COUNTRY         STRING,
            PLACING         INTEGER,
            WINS            INTEGER,
            LOSSES          INTEGER,
            TIES            INTEGER,
            DECK_ID         STRING,
            DECK_NAME       STRING,
            DECK_ICONS      VARIANT,
            DROP_ROUND      INTEGER,
            _LOADED_AT      TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
        )
    """,
    "MATCHES": """
        CREATE TABLE IF NOT EXISTS PTCG_SCOUTING.RAW.MATCHES (
            TOURNAMENT_ID STRING   NOT NULL,
            ROUND         INTEGER,
            PHASE         INTEGER,
            TABLE_NUMBER  INTEGER,
            MATCH_ID      STRING,
            PLAYER1       STRING,
            PLAYER2       STRING,
            WINNER        STRING,
            _LOADED_AT    TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
        )
    """,
    "DECKLISTS": """
        CREATE TABLE IF NOT EXISTS PTCG_SCOUTING.RAW.DECKLISTS (
            TOURNAMENT_ID   STRING  NOT NULL,
            PLAYER_USERNAME STRING  NOT NULL,
            CARD_CATEGORY   STRING,
            CARD_NAME       STRING,
            CARD_SET        STRING,
            CARD_NUMBER     STRING,
            CARD_COUNT      INTEGER,
            _LOADED_AT      TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
        )
    """,
}


def get_conn():
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        database=os.environ.get("SNOWFLAKE_DATABASE", "PTCG_SCOUTING"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "SYSADMIN"),
    )


def ensure_schema(cur):
    cur.execute("CREATE DATABASE IF NOT EXISTS PTCG_SCOUTING")
    cur.execute("CREATE SCHEMA IF NOT EXISTS PTCG_SCOUTING.RAW")
    for ddl in DDL.values():
        cur.execute(ddl)
    log.info("Schema ready")


def load_tournaments(cur, client: LimitlessClient, max_pages: int = 3) -> int:
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
            cur.execute(
                """
                MERGE INTO PTCG_SCOUTING.RAW.TOURNAMENTS tgt
                USING (SELECT %s AS ID) src ON tgt.ID = src.ID
                WHEN NOT MATCHED THEN
                  INSERT (ID, GAME, FORMAT, NAME, TOURNAMENT_DATE, PLAYER_COUNT)
                  VALUES (%s, %s, %s, %s, %s::TIMESTAMP_TZ, %s)
                """,
                (t["id"], t["id"], t.get("game"), t.get("format"), t.get("name"), t.get("date"), t.get("players")),
            )
        total += len(page_rows)
        log.info(f"  page {page}: {len(page_rows)} tournaments ({total} total)")
        if hit_cutoff or len(rows) < 100:
            break
    return total


def load_tournament_data(cur, client: LimitlessClient, tournament_id: str):
    standings = client.get_standings(tournament_id)
    for s in standings:
        record = s.get("record") or {}
        deck = s.get("deck") or {}
        cur.execute(
            """
            INSERT INTO PTCG_SCOUTING.RAW.STANDINGS
              (TOURNAMENT_ID, PLAYER_USERNAME, PLAYER_NAME, COUNTRY,
               PLACING, WINS, LOSSES, TIES,
               DECK_ID, DECK_NAME, DECK_ICONS, DROP_ROUND)
            SELECT %s, %s, %s, %s,
                   %s, %s, %s, %s,
                   %s, %s, PARSE_JSON(%s), %s
            """,
            (
                tournament_id,
                s.get("player"),
                s.get("name"),
                s.get("country"),
                s.get("placing"),
                record.get("wins"),
                record.get("losses"),
                record.get("ties"),
                deck.get("id"),
                deck.get("name"),
                json.dumps(deck.get("icons", [])),
                s.get("drop"),
            ),
        )

    # deck lists (available without API key)
    for s in standings:
        username = s.get("player")
        decklist = s.get("decklist") or {}
        for category in ("pokemon", "trainer", "energy"):
            for card in decklist.get(category, []):
                cur.execute(
                    """
                    INSERT INTO PTCG_SCOUTING.RAW.DECKLISTS
                      (TOURNAMENT_ID, PLAYER_USERNAME, CARD_CATEGORY,
                       CARD_NAME, CARD_SET, CARD_NUMBER, CARD_COUNT)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tournament_id,
                        username,
                        category,
                        card.get("name"),
                        card.get("set"),
                        card.get("number"),
                        card.get("count"),
                    ),
                )

    pairings = client.get_pairings(tournament_id)
    for p in pairings:
        winner = p.get("winner")
        cur.execute(
            """
            INSERT INTO PTCG_SCOUTING.RAW.MATCHES
              (TOURNAMENT_ID, ROUND, PHASE, TABLE_NUMBER, MATCH_ID, PLAYER1, PLAYER2, WINNER)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tournament_id,
                p.get("round"),
                p.get("phase"),
                p.get("table"),
                p.get("match"),
                p.get("player1"),
                p.get("player2"),
                str(winner) if winner is not None else None,
            ),
        )

    log.info(f"  {tournament_id}: {len(standings)} standings, {len(pairings)} pairings")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tournament", help="Load a single tournament by ID")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, no DB writes")
    parser.add_argument("--pages", type=int, default=3, help="Max pages to scan when indexing tournaments (default: 3 for incremental runs). Use --pages 999 for a full historical load.")
    args = parser.parse_args()

    client = LimitlessClient(api_key=os.environ.get("LIMITLESS_API_KEY"))

    if args.dry_run:
        rows = client.get_tournaments(limit=5)
        log.info(f"Dry run: fetched {len(rows)} tournaments — first: {rows[0]}")
        return

    conn = get_conn()
    cur = conn.cursor()

    try:
        ensure_schema(cur)

        if args.tournament:
            tournament_ids = [args.tournament]
        else:
            n = load_tournaments(cur, client, max_pages=args.pages)
            log.info(f"Tournament index: {n} loaded")
            cur.execute("""
                SELECT ID FROM PTCG_SCOUTING.RAW.TOURNAMENTS
                WHERE GAME = 'PTCG'
                  AND PLAYER_COUNT >= 64
                  AND TOURNAMENT_DATE >= DATEADD('year', -3, CURRENT_DATE())
                ORDER BY TOURNAMENT_DATE DESC
            """)
            tournament_ids = [r[0] for r in cur.fetchall()]

        log.info(f"Loading data for {len(tournament_ids)} tournaments...")
        for tid in tournament_ids:
            cur.execute("SELECT COUNT(*) FROM PTCG_SCOUTING.RAW.STANDINGS WHERE TOURNAMENT_ID = %s", (tid,))
            if cur.fetchone()[0] > 0 and not args.tournament:
                continue
            try:
                load_tournament_data(cur, client, tid)
            except Exception as e:
                log.warning(f"  {tid}: skipped — {e}")

        conn.commit()
        log.info("Done")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
