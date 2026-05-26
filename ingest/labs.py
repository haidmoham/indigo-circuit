"""
Ingest major tournament data from Limitless Labs into DuckDB.

Labs covers official Play Pokémon events (Regionals, ICs, Worlds) sourced
from RK9. Tournaments are curated in data/major_tournaments.json.

Usage:
    python ingest/labs.py                         # load all curated tournaments
    python ingest/labs.py --season 2026           # load one season only
    python ingest/labs.py --id 0063               # reload a single tournament
    python ingest/labs.py --dry-run               # fetch only, print sample
    python ingest/labs.py --with-decklists        # also scrape match+decklist data (top 64)
    python ingest/labs.py --with-decklists --top-n 128  # scrape top 128 per tournament
    python ingest/labs.py --id 0063 --with-decklists --force  # re-scrape even if cached
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
# Scraping — standings
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
# Scraping — player matches + decklists
# ---------------------------------------------------------------------------

def fetch_player_matches(tournament_id: str, player_id: str) -> list[dict]:
    """Round-by-round results for a player. Returns [] on any failure."""
    url = f"{LABS_BASE}/{tournament_id}/player/{player_id}"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        time.sleep(0.4)
    except requests.RequestException as e:
        log.warning(f"    match fetch failed {tournament_id}/{player_id}: {e}")
        return []

    soup = BeautifulSoup(resp.content, "html.parser")
    rows = []

    for tr in soup.select("table tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue

        # Round number — first cell that's a bare integer
        round_num = None
        for cell in cells[:2]:
            t = cell.get_text(strip=True)
            if re.match(r"^\d+$", t):
                round_num = int(t)
                break

        # Result — look for Win/Loss/Tie/Bye text
        result = None
        for cell in cells:
            t = cell.get_text(strip=True)
            if t in ("Win", "Loss", "Tie", "Bye"):
                result = t
                break

        if not result:
            continue

        if result == "Bye":
            rows.append({
                "round": round_num, "result": "Bye",
                "opponent_name": None, "opponent_deck_id": None, "opponent_deck_name": None,
            })
            continue

        # Opponent name — a link to another player page in this tournament
        opponent_name = None
        opponent_deck_id = None
        opponent_deck_name = None

        for cell in cells:
            # Player link: /{tournament_id}/player/{id}
            for a in cell.find_all("a", href=True):
                href = a["href"]
                if re.search(rf"/{re.escape(tournament_id)}/player/\d+$", href):
                    opponent_name = a.get_text(strip=True) or None
                # Deck link: /decks/{deck_id}
                if re.search(r"/decks/[^/]+$", href) and not href.endswith("/list"):
                    opponent_deck_id = href.rstrip("/").split("/")[-1] or None
                    deck_img = cell.find("img")
                    if deck_img:
                        opponent_deck_name = deck_img.get("alt", "").strip() or None

        rows.append({
            "round":              round_num,
            "result":             result,
            "opponent_name":      opponent_name,
            "opponent_deck_id":   opponent_deck_id,
            "opponent_deck_name": opponent_deck_name,
        })

    return rows


def fetch_player_decklist(tournament_id: str, player_id: str) -> list[dict]:
    """Full card list for a player. Returns [] if not available (404) or unparseable."""
    url = f"{LABS_BASE}/{tournament_id}/player/{player_id}/decklist"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        time.sleep(0.4)
    except requests.RequestException as e:
        log.warning(f"    decklist fetch failed {tournament_id}/{player_id}: {e}")
        return []

    soup = BeautifulSoup(resp.content, "html.parser")
    cards = []
    current_category = None

    # Walk every element; track category headers and parse card rows/items
    for el in soup.find_all(True):
        tag = el.name.lower()

        # Category headers (h2, h3, div with category text, th)
        if tag in ("h2", "h3", "th"):
            txt = el.get_text(strip=True).lower()
            if "pokémon" in txt or "pokemon" in txt:
                current_category = "pokemon"
            elif "trainer" in txt:
                current_category = "trainer"
            elif "energy" in txt:
                current_category = "energy"
            continue

        if not current_category:
            continue

        # Table row approach
        if tag == "tr":
            cells = el.find_all("td")
            if len(cells) < 2:
                continue
            card_count, card_name, card_set, card_number = _parse_card_cells(cells)
            if card_name and card_count:
                cards.append({
                    "card_category": current_category,
                    "card_name":     card_name,
                    "card_set":      card_set,
                    "card_number":   card_number,
                    "card_count":    card_count,
                })

        # List item approach: "4x Dreepy (ASC 158)" or "4 Dreepy ASC 158"
        if tag == "li":
            parsed = _parse_card_text(el.get_text(strip=True))
            if parsed:
                parsed["card_category"] = current_category
                cards.append(parsed)

    return cards


def _parse_card_cells(cells) -> tuple:
    """Extract (count, name, set, number) from a list of <td> elements."""
    card_count = None
    card_name  = None
    card_set   = None
    card_number = None

    for cell in cells:
        txt = cell.get_text(strip=True)

        # Pure integer → count
        if re.match(r"^\d+$", txt) and card_count is None:
            card_count = int(txt)
            continue

        # Card link or bolded name
        link = cell.find("a")
        if link and not card_name:
            card_name = link.get_text(strip=True) or None

        # Set/number pattern: "ASC 158" or "ASC158"
        m = re.search(r"\b([A-Z]{2,6})\s*(\d+[A-Z]?)\b", txt)
        if m and card_set is None:
            card_set   = m.group(1)
            card_number = m.group(2)

        # Fallback name: any non-empty non-number cell
        if not card_name and txt and not re.match(r"^[\d\s]+$", txt):
            card_name = txt

    return card_count, card_name, card_set, card_number


def _parse_card_text(text: str) -> dict | None:
    """
    Parse card text like:
      "4x Dreepy (ASC 158)"
      "×4 Dreepy ASC 158"
      "4 Dreepy ASC 158"
    Returns None if unparseable.
    """
    # Count at start or end
    m = re.match(r"^[×x]?(\d+)[×x]?\s+(.+)$", text, re.IGNORECASE)
    if not m:
        m = re.match(r"^(.+?)\s+[×x](\d+)$", text, re.IGNORECASE)
        if m:
            name_part, count_str = m.group(1), m.group(2)
        else:
            return None
    else:
        count_str, name_part = m.group(1), m.group(2)

    card_count = int(count_str)

    # Strip trailing set/number from name: "Dreepy (ASC 158)" or "Dreepy ASC 158"
    set_m = re.search(r"\s*\(?([A-Z]{2,6})\s+(\d+[A-Z]?)\)?$", name_part)
    card_set = card_number = None
    if set_m:
        card_set    = set_m.group(1)
        card_number = set_m.group(2)
        name_part   = name_part[:set_m.start()].strip().rstrip("(").strip()

    card_name = name_part.strip() or None
    return {
        "card_name":    card_name,
        "card_set":     card_set,
        "card_number":  card_number,
        "card_count":   card_count,
    } if card_name else None


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
            "placing"           INTEGER,
            wins                INTEGER,
            losses              INTEGER,
            ties                INTEGER,
            deck_id             VARCHAR,
            deck_name           VARCHAR,
            deck_sprite         VARCHAR,
            _loaded_at          TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.major_matches (
            labs_tournament_id  VARCHAR NOT NULL,
            player_id           VARCHAR NOT NULL,
            round_number        INTEGER,
            result              VARCHAR,
            opponent_name       VARCHAR,
            opponent_deck_id    VARCHAR,
            opponent_deck_name  VARCHAR,
            _loaded_at          TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.major_decklists (
            labs_tournament_id  VARCHAR NOT NULL,
            player_id           VARCHAR NOT NULL,
            card_category       VARCHAR,
            card_name           VARCHAR NOT NULL,
            card_set            VARCHAR,
            card_number         VARCHAR,
            card_count          INTEGER,
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
           "placing", wins, losses, ties, deck_id, deck_name, deck_sprite)
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


def already_scraped_players(conn, tournament_id: str) -> set[str]:
    """Return player_ids that already have match data for this tournament."""
    rows = conn.execute(
        "SELECT DISTINCT player_id FROM raw.major_matches WHERE labs_tournament_id = ?",
        (tournament_id,)
    ).fetchall()
    return {r[0] for r in rows}


def load_player_matches(conn, tournament_id: str, player_id: str, matches: list[dict]):
    conn.execute(
        "DELETE FROM raw.major_matches WHERE labs_tournament_id = ? AND player_id = ?",
        (tournament_id, player_id)
    )
    if not matches:
        return
    conn.executemany("""
        INSERT INTO raw.major_matches
          (labs_tournament_id, player_id, round_number, result,
           opponent_name, opponent_deck_id, opponent_deck_name)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        (tournament_id, player_id,
         m["round"], m["result"],
         m["opponent_name"], m["opponent_deck_id"], m["opponent_deck_name"])
        for m in matches
    ])


def load_player_decklist(conn, tournament_id: str, player_id: str, cards: list[dict]):
    conn.execute(
        "DELETE FROM raw.major_decklists WHERE labs_tournament_id = ? AND player_id = ?",
        (tournament_id, player_id)
    )
    if not cards:
        return
    conn.executemany("""
        INSERT INTO raw.major_decklists
          (labs_tournament_id, player_id, card_category, card_name,
           card_set, card_number, card_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        (tournament_id, player_id,
         c["card_category"], c["card_name"],
         c.get("card_set"), c.get("card_number"), c["card_count"])
        for c in cards
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
    parser.add_argument("--with-decklists", action="store_true",
                        help="Also scrape round-by-round matches and full decklists for top-N players")
    parser.add_argument("--top-n", type=int, default=64,
                        help="How many top finishers to scrape per tournament (default 64)")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even if match/decklist data already exists")
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

            if not args.with_decklists:
                continue

            # Scrape top-N players' matches + decklists
            top_players = [
                s for s in standings
                if s["player_id"] and s["placing"] and s["placing"] <= args.top_n
            ]

            already = set() if args.force else already_scraped_players(conn, t["id"])
            to_scrape = [p for p in top_players if p["player_id"] not in already]

            if not to_scrape:
                log.info(f"    top-{args.top_n} already scraped, skipping")
                continue

            log.info(f"    scraping {len(to_scrape)} players (top {args.top_n}, {len(already)} cached)...")

            dl_count = match_count = 0
            for p in to_scrape:
                pid  = p["player_id"]
                name = p["player_name"]

                matches = fetch_player_matches(t["id"], pid)
                load_player_matches(conn, t["id"], pid, matches)
                if matches:
                    match_count += 1

                decklist = fetch_player_decklist(t["id"], pid)
                load_player_decklist(conn, t["id"], pid, decklist)
                if decklist:
                    dl_count += 1

                log.debug(f"      #{p['placing']:3d} {name}: {len(matches)} rounds, {len(decklist)} cards")

            conn.commit()
            log.info(f"    {match_count}/{len(to_scrape)} match pages, {dl_count}/{len(to_scrape)} decklists")

        conn.commit()
        log.info("Done")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
