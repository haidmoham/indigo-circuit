"""
Ingest major tournament data from Limitless Labs into Snowflake.

Labs covers official Play Pokémon events (Regionals, ICs, Worlds) sourced
from RK9. Tournaments are curated in data/major_tournaments.json.

Usage:
    python ingest/labs.py                    # load all curated tournaments
    python ingest/labs.py --season 2026      # load one season only
    python ingest/labs.py --id 0063          # reload a single tournament
    python ingest/labs.py --dry-run          # fetch only, print sample
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests
import snowflake.connector
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

LABS_BASE = "https://labs.limitlesstcg.com"
DATA_DIR = Path(__file__).parent.parent / "data"

TIER_BONUS = {
    "worlds":        3.0,
    "international": 2.0,
    "regional":      1.5,
    "special":       1.1,
}

SEASON_WEIGHT = {
    "2026": 1.0,
    "2025": 0.5,
}


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def fetch_standings(tournament_id: str) -> list[dict]:
    url = f"{LABS_BASE}/{tournament_id}/standings"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    time.sleep(0.5)  # be polite

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = []

    for tr in soup.select("table tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue

        placing_text = cells[0].get_text(strip=True)
        placing = int(re.sub(r"\D", "", placing_text)) if re.sub(r"\D", "", placing_text) else None

        # player name + player_id from href
        name_link = cells[1].find("a")
        player_name = name_link.get_text(strip=True) if name_link else cells[1].get_text(strip=True)
        player_href = name_link["href"] if name_link else ""
        player_id = player_href.split("/")[-1] if player_href else None

        # country from flag img title
        flag_img = cells[1].find("img")
        country = flag_img["title"] if flag_img and flag_img.get("title") else None

        # record: search all cells for pattern "W - L - T" or "W-L-T"
        wins = losses = ties = None
        record_pat = re.compile(r"^(\d+)\s*[-–]\s*(\d+)\s*[-–]\s*(\d+)$")
        for cell in cells:
            m = record_pat.match(cell.get_text(strip=True))
            if m:
                wins, losses, ties = int(m.group(1)), int(m.group(2)), int(m.group(3))
                break

        # deck archetype: find the cell with a /decks/ link
        deck_id = None
        deck_name = None
        for cell in cells:
            deck_link = cell.find("a", href=re.compile(r"/decks/"))
            if deck_link:
                deck_href = deck_link.get("href", "")
                deck_id = deck_href.rstrip("/").split("/")[-1] or None
                deck_img = cell.find("img")
                if deck_img:
                    deck_name = deck_img.get("alt", "").strip() or None
                break

        rows.append({
            "player_id":   player_id,
            "player_name": player_name,
            "country":     country,
            "placing":     placing,
            "wins":        wins,
            "losses":      losses,
            "ties":        ties,
            "deck_id":     deck_id,
            "deck_name":   deck_name,
        })

    return rows


# ---------------------------------------------------------------------------
# Snowflake
# ---------------------------------------------------------------------------

def get_conn():
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        database="PTCG_SCOUTING",
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
    )


def ensure_schema(cur):
    cur.execute("CREATE SCHEMA IF NOT EXISTS PTCG_SCOUTING.RAW")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PTCG_SCOUTING.RAW.MAJOR_TOURNAMENTS (
            LABS_ID        STRING  NOT NULL,
            NAME           STRING,
            TOURNAMENT_DATE DATE,
            LOCATION       STRING,
            TIER           STRING,
            SEASON         STRING,
            TIER_BONUS     FLOAT,
            SEASON_WEIGHT  FLOAT,
            _LOADED_AT     TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP(),
            CONSTRAINT PK_MAJOR_TOURNAMENTS PRIMARY KEY (LABS_ID)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PTCG_SCOUTING.RAW.MAJOR_STANDINGS (
            LABS_TOURNAMENT_ID  STRING  NOT NULL,
            PLAYER_ID           STRING,
            PLAYER_NAME         STRING,
            COUNTRY             STRING,
            PLACING             INTEGER,
            WINS                INTEGER,
            LOSSES              INTEGER,
            TIES                INTEGER,
            DECK_ID             STRING,
            DECK_NAME           STRING,
            _LOADED_AT          TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)
    log.info("Major schema ready")


def upsert_tournament(cur, t: dict):
    cur.execute("""
        MERGE INTO PTCG_SCOUTING.RAW.MAJOR_TOURNAMENTS tgt
        USING (SELECT %s AS LABS_ID) src ON tgt.LABS_ID = src.LABS_ID
        WHEN NOT MATCHED THEN INSERT
          (LABS_ID, NAME, TOURNAMENT_DATE, LOCATION, TIER, SEASON, TIER_BONUS, SEASON_WEIGHT)
          VALUES (%s, %s, %s::DATE, %s, %s, %s, %s, %s)
    """, (
        t["id"], t["id"], t["name"], t["date"],
        t["location"], t["tier"], t["season"],
        TIER_BONUS.get(t["tier"], 1.0),
        SEASON_WEIGHT.get(t["season"], 0.25),
    ))


def load_standings(cur, tournament_id: str, standings: list[dict]):
    # clear existing rows for this tournament before reloading
    cur.execute("DELETE FROM PTCG_SCOUTING.RAW.MAJOR_STANDINGS WHERE LABS_TOURNAMENT_ID = %s", (tournament_id,))
    for s in standings:
        cur.execute("""
            INSERT INTO PTCG_SCOUTING.RAW.MAJOR_STANDINGS
              (LABS_TOURNAMENT_ID, PLAYER_ID, PLAYER_NAME, COUNTRY,
               PLACING, WINS, LOSSES, TIES, DECK_ID, DECK_NAME)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            tournament_id,
            s["player_id"], s["player_name"], s["country"],
            s["placing"], s["wins"], s["losses"], s["ties"],
            s["deck_id"], s["deck_name"],
        ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_curated() -> list[dict]:
    with open(DATA_DIR / "major_tournaments.json") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", help="Only load this season (e.g. 2026)")
    parser.add_argument("--id", help="Load a single tournament by Labs ID (e.g. 0063)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tournaments = load_curated()

    if args.id:
        tournaments = [t for t in tournaments if t["id"] == args.id]
    elif args.season:
        tournaments = [t for t in tournaments if t["season"] == args.season]

    if not tournaments:
        log.error("No tournaments matched — check --id or --season value")
        sys.exit(1)

    log.info(f"Processing {len(tournaments)} tournament(s)...")

    if args.dry_run:
        sample = fetch_standings(tournaments[0]["id"])
        print(f"\n{tournaments[0]['name']} — {len(sample)} standings")
        for s in sample[:5]:
            print(f"  #{s['placing']:3d} {s['player_name']:<25} {s['wins']}-{s['losses']}-{s['ties']}  {s['deck_name'] or '—'}")
        return

    conn = get_conn()
    cur = conn.cursor()
    try:
        ensure_schema(cur)

        for t in tournaments:
            upsert_tournament(cur, t)
            log.info(f"  {t['id']} {t['name']}")
            standings = fetch_standings(t["id"])
            load_standings(cur, t["id"], standings)
            log.info(f"    {len(standings)} standings loaded")

        conn.commit()
        log.info("Done")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
