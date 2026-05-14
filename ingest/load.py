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
        CREATE TABLE IF NOT EXISTS RAW.PTCG.TOURNAMENTS (
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
        CREATE TABLE IF NOT EXISTS RAW.PTCG.STANDINGS (
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
        CREATE TABLE IF NOT EXISTS RAW.PTCG.MATCHES (
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
    cur.execute("CREATE SCHEMA IF NOT EXISTS PTCG_SCOUTING.PTCG")
    for ddl in DDL.values():
        cur.execute(ddl)
    log.info("Schema ready")


def load_tournaments(cur, client: LimitlessClient) -> int:
    total = 0
    for page in range(1, 50):
        rows = client.get_tournaments(limit=100, page=page)
        if not rows:
            break
        for t in rows:
            cur.execute(
                """
                MERGE INTO RAW.PTCG.TOURNAMENTS tgt
                USING (SELECT %s AS ID) src ON tgt.ID = src.ID
                WHEN NOT MATCHED THEN
                  INSERT (ID, GAME, FORMAT, NAME, TOURNAMENT_DATE, PLAYER_COUNT)
                  VALUES (%s, %s, %s, %s::TIMESTAMP_TZ, %s)
                """,
                (t["id"], t["id"], t.get("game"), t.get("format"), t.get("name"), t.get("date"), t.get("players")),
            )
        total += len(rows)
        log.info(f"  page {page}: {len(rows)} tournaments ({total} total)")
        if len(rows) < 100:
            break
    return total


def load_tournament_data(cur, client: LimitlessClient, tournament_id: str):
    standings = client.get_standings(tournament_id)
    for s in standings:
        record = s.get("record") or {}
        deck = s.get("deck") or {}
        cur.execute(
            """
            INSERT INTO RAW.PTCG.STANDINGS
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

    pairings = client.get_pairings(tournament_id)
    for p in pairings:
        winner = p.get("winner")
        cur.execute(
            """
            INSERT INTO RAW.PTCG.MATCHES
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
            n = load_tournaments(cur, client)
            log.info(f"Tournament index: {n} loaded")
            cur.execute("SELECT ID FROM RAW.PTCG.TOURNAMENTS WHERE GAME = 'PTCG' ORDER BY TOURNAMENT_DATE DESC")
            tournament_ids = [r[0] for r in cur.fetchall()]

        log.info(f"Loading data for {len(tournament_ids)} tournaments...")
        for tid in tournament_ids:
            cur.execute("SELECT COUNT(*) FROM RAW.PTCG.STANDINGS WHERE TOURNAMENT_ID = %s", (tid,))
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
