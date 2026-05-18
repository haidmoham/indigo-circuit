"""
Ingest major tournament data from Limitless Labs into DuckDB.

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

import duckdb
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

LABS_BASE = "https://labs.limitlesstcg.com"
DATA_DIR = Path(__file__).parent.parent / "data"
DUCKDB_PATH = os.environ.get("DUCKDB_PATH", str(Path(__file__).parent.parent / "data" / "ptcg.duckdb"))

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

    soup = BeautifulSoup(resp.content, "html.parser")
    rows = []

    for tr in soup.select("table tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue

        placing_text = cells[0].get_text(strip=True)
        placing = int(re.sub(r"\D", "", placing_text)) if re.sub(r"\D", "", placing_text) else None

        name_link = cells[1].find("a")
        player_name = name_link.get_text(strip=True) if name_link else cells[1].get_text(strip=True)
        player_href = name_link["href"] if name_link else ""
        player_id = player_href.split("/")[-1] if player_href else None

        flag_img = cells[1].find("img")
        country = flag_img["title"] if flag_img and flag_img.get("title") else None

        wins = losses = ties = None
        record_pat = re.compile(r"^(\d+)\s*[-–]\s*(\d+)\s*[-–]\s*(\d+)$")
        for cell in cells:
            m = record_pat.match(cell.get_text(strip=True))
            if m:
                wins, losses, ties = int(m.group(1)), int(m.group(2)), int(m.group(3))
                break

        deck_id = None
        deck_name = None
        deck_sprite = None
        for cell in cells:
            deck_link = cell.find("a", href=re.compile(r"/decks/"))
            if deck_link:
                deck_href = deck_link.get("href", "")
                deck_id = deck_href.rstrip("/").split("/")[-1] or None
                deck_img = cell.find("img")
                if deck_img:
                    deck_name   = deck_img.get("alt", "").strip() or None
                    deck_sprite = deck_img.get("src", "").strip() or None
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
            "deck_sprite": deck_sprite,
        })

    return rows


# ---------------------------------------------------------------------------
# DuckDB
# ---------------------------------------------------------------------------

def get_conn():
    return duckdb.connect(DUCKDB_PATH)


def ensure_schema(conn):
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.major_tournaments (
            labs_id         VARCHAR  PRIMARY KEY,
            name            VARCHAR,
            tournament_date DATE,
            location        VARCHAR,
            tier            VARCHAR,
            season          VARCHAR,
            tier_bonus      DOUBLE,
            season_weight   DOUBLE,
            _loaded_at      TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.major_standings (
            labs_tournament_id  VARCHAR  NOT NULL,
            player_id           VARCHAR,
            player_name         VARCHAR,
            country             VARCHAR,
            placing             INTEGER,
            wins                INTEGER,
            losses              INTEGER,
            ties                INTEGER,
            deck_id             VARCHAR,
            deck_name           VARCHAR,
            deck_sprite         VARCHAR,
            _loaded_at          TIMESTAMPTZ DEFAULT now()
        )
    """)
    log.info("Major schema ready")


def upsert_tournament(conn, t: dict):
    conn.execute("""
        INSERT INTO raw.major_tournaments
          (labs_id, name, tournament_date, location, tier, season, tier_bonus, season_weight)
        VALUES (?, ?, ?::DATE, ?, ?, ?, ?, ?)
        ON CONFLICT (labs_id) DO NOTHING
    """, (
        t["id"], t["name"], t["date"],
        t["location"], t["tier"], t["season"],
        TIER_BONUS.get(t["tier"], 1.0),
        SEASON_WEIGHT.get(t["season"], 0.25),
    ))


def load_standings(conn, tournament_id: str, standings: list[dict]):
    conn.execute("DELETE FROM raw.major_standings WHERE labs_tournament_id = ?", (tournament_id,))
    conn.executemany("""
        INSERT INTO raw.major_standings
          (labs_tournament_id, player_id, player_name, country,
           placing, wins, losses, ties, deck_id, deck_name, deck_sprite)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            tournament_id,
            s["player_id"], s["player_name"], s["country"],
            s["placing"], s["wins"], s["losses"], s["ties"],
            s["deck_id"], s["deck_name"], s["deck_sprite"],
        )
        for s in standings
    ])


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
    try:
        ensure_schema(conn)

        for t in tournaments:
            upsert_tournament(conn, t)
            log.info(f"  {t['id']} {t['name']}")
            standings = fetch_standings(t["id"])
            load_standings(conn, t["id"], standings)
            log.info(f"    {len(standings)} standings loaded")

        conn.commit()
        log.info("Done")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
