"""
Ingest major tournament data from Limitless Labs into DuckDB.

Labs covers official Play Pokémon events (Regionals, ICs, Worlds) sourced
from RK9. Tournaments are curated in data/major_tournaments.json.

Tournaments are seeded from data/major_tournaments.json and auto-discovered from
the Labs index (--discover). The DB (raw.major_tournaments) is the source of truth
for the scrape work-list, so discovered events persist across deploys.

Usage:
    python ingest/labs.py                              # load all known tournaments
    python ingest/labs.py --discover                   # + find & add new ones from Labs
    python ingest/labs.py --season 2026                # load one season only
    python ingest/labs.py --current-rotation           # only post-April-1 events
    python ingest/labs.py --id 0063                    # reload a single tournament
    python ingest/labs.py --dry-run                    # fetch only, print sample
    python ingest/labs.py --with-decklists             # scrape match+decklist data (top 64)
    python ingest/labs.py --with-decklists --top-n 128 # scrape top 128 per tournament
    python ingest/labs.py --with-decklists --all-players  # scrape full field (all standings)
    python ingest/labs.py --discover --with-decklists --current-rotation  # nightly auto-scrape
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
# Auto-discovery — find new tournaments from the Labs index page
# ---------------------------------------------------------------------------
_MONTHS = {}
for _i, (_full, _abbr) in enumerate([
    ("January", "Jan"), ("February", "Feb"), ("March", "Mar"), ("April", "Apr"),
    ("May", "May"), ("June", "Jun"), ("July", "Jul"), ("August", "Aug"),
    ("September", "Sep"), ("October", "Oct"), ("November", "Nov"), ("December", "Dec"),
], 1):
    _MONTHS[_full.lower()] = _i
    _MONTHS[_abbr.lower()] = _i


def _parse_date_range(text: str) -> str | None:
    """Parse a Labs date label to the event's END date as ISO 'YYYY-MM-DD'.

    Handles full + abbreviated month names, same-month ranges ('April 25-26'),
    cross-month ranges ('February 27-March 1'), and single days ('May 16').
    """
    t = text.replace("–", "-").replace("—", "-").strip()
    ym = re.search(r"(\d{4})", t)
    if not ym:
        return None
    year = int(ym.group(1))
    core = t[: ym.start()].strip().rstrip(",").strip()
    segs = [s.strip() for s in core.split("-")]

    def _month_day(seg, fallback_month=None):
        mm = re.search(r"([A-Za-z]+)", seg)
        dd = re.search(r"(\d{1,2})", seg)
        month = _MONTHS.get(mm.group(1).lower()) if mm else fallback_month
        day = int(dd.group(1)) if dd else None
        return month, day

    sm, sd = _month_day(segs[0])
    em, ed = _month_day(segs[-1], fallback_month=sm)
    month, day = (em or sm), (ed or sd)
    if not month or not day:
        return None
    return f"{year:04d}-{month:02d}-{int(day):02d}"


def _classify_tier(name: str, logo_src: str = "") -> str:
    """Infer tier from the tournament name and logo path (regional/special/...)."""
    hay = (logo_src + " " + name).lower()
    if "world" in hay:
        return "worlds"
    if "international" in hay:
        return "international"
    if "special" in hay:
        return "special"
    if "regional" in hay:
        return "regional"
    return "other"


def _season_for_date(iso_date: str) -> str:
    """PTCG competitive season rolls over in September; season = later year."""
    y, m = int(iso_date[:4]), int(iso_date[5:7])
    return str(y + 1 if m >= 9 else y)


def _rotation_cutoff() -> str:
    """ISO date of the most recent April 1 — the current Standard rotation start.
    Mirrors dashboard/app.py _rotation_cutoff() so ingest and serving agree."""
    from datetime import date
    today = date.today()
    year = today.year if (today.month, today.day) >= (4, 1) else today.year - 1
    return f"{year}-04-01"


def discover_tournaments() -> list[dict]:
    """Scrape the Labs index page for every listed tournament and parse its
    metadata. Returns curated-shaped dicts: {id, name, date, location, tier, season}.

    The index lists tournaments newest-first, so brand-new events appear here
    within hours of results posting — no manual JSON edit required.
    """
    resp = requests.get(f"{LABS_BASE}/", timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
    out: list[dict] = []
    for a in soup.find_all("a", href=re.compile(r"^/\d{4}")):
        m = re.match(r"^/(\d{4})", a.get("href", ""))
        if not m:
            continue
        tid = m.group(1)
        name_el = a.find("div", class_=re.compile(r"font-bold"))
        name = name_el.get_text(strip=True) if name_el else None
        if not name:
            continue
        logo = a.find("img", alt=re.compile("logo"))
        logo_src = logo.get("src", "") if logo else ""
        flag = a.find("img", src=re.compile(r"/flags/"))
        country = (flag.get("title") or flag.get("alt")) if flag else None
        date_div = a.find("div", class_=re.compile("items-center"))
        iso = _parse_date_range(date_div.get_text(" ", strip=True) if date_div else "")
        if not iso:
            log.warning(f"  discovery: could not parse date for {tid} {name}")
            continue
        out.append({
            "id": tid, "name": name, "date": iso, "location": country,
            "tier": _classify_tier(name, logo_src), "season": _season_for_date(iso),
        })
    return out


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

    # Labs injects decklist as JSON inside a <script> tag.
    # Structure: {"status":200,...,"body":"<json-string>"}
    # where the body JSON has {"ok":true,"message":{"pokemon":[...],"trainer":[...],"energy":[...]}}
    import json as _json
    for script in soup.find_all("script"):
        txt = script.string or ""
        if not txt.strip().startswith("{"):
            continue
        try:
            outer = _json.loads(txt)
            body_str = outer.get("body", "{}")
            inner = _json.loads(body_str) if isinstance(body_str, str) else body_str
            message = inner.get("message", {})
            if not isinstance(message, dict) or "pokemon" not in message:
                continue
            cards = []
            for category in ("pokemon", "trainer", "energy"):
                for c in message.get(category, []):
                    name = c.get("name", "").strip()
                    if name:
                        cards.append({
                            "card_category": category,
                            "card_name":     name,
                            "card_set":      c.get("set"),
                            "card_number":   str(c.get("number", "")) or None,
                            "card_count":    int(c.get("count", 1)),
                        })
            if cards:
                return cards
        except Exception:
            pass

    # Fallback: walk HTML elements for table/list-based layouts
    cards = []
    current_category = None
    for el in soup.find_all(True):
        tag = el.name.lower()
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
        if tag == "tr":
            cells = el.find_all("td")
            if len(cells) < 2:
                continue
            card_count, card_name, card_set, card_number = _parse_card_cells(cells)
            if card_name and card_count:
                cards.append({"card_category": current_category, "card_name": card_name,
                               "card_set": card_set, "card_number": card_number, "card_count": card_count})
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
    # A player is fully cached only if they have BOTH match data AND decklist data.
    # Players with matches but no decklists need to be re-scraped after the parser fix.
    rows = conn.execute("""
        SELECT DISTINCT player_id FROM raw.major_matches
        WHERE labs_tournament_id = ?
          AND player_id IN (
              SELECT DISTINCT player_id FROM raw.major_decklists
              WHERE labs_tournament_id = ?
          )
    """, (tournament_id, tournament_id)).fetchall()
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
    parser.add_argument("--discover", action="store_true",
                        help="Scrape the Labs index for new tournaments and add them to "
                             "the registry before scraping. Requires no manual JSON edits.")
    parser.add_argument("--current-rotation", action="store_true",
                        help="Only process tournaments on/after the most recent April 1 "
                             "rotation cutoff (the currently-legal Standard pool).")
    parser.add_argument("--with-decklists", action="store_true",
                        help="Also scrape round-by-round matches and full decklists for top-N players")
    parser.add_argument("--top-n", type=int, default=64,
                        help="How many top finishers to scrape per tournament (default 64)")
    parser.add_argument("--all-players", action="store_true",
                        help="Scrape the full field (all standings rows with a player_id). "
                             "Supersedes --top-n. Incremental: already-cached players are skipped.")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even if match/decklist data already exists")
    args = parser.parse_args()

    # --- Dry-run: side-effect-free preview from the curated list (+ optional discovery) ---
    if args.dry_run:
        pool = {t["id"]: t for t in load_curated()}
        if args.discover:
            try:
                for t in discover_tournaments():
                    pool.setdefault(t["id"], t)
            except Exception as e:
                log.warning(f"Discovery failed: {e}")
        pool = list(pool.values())
        if args.id:
            pool = [t for t in pool if t["id"] == args.id]
        elif args.season:
            pool = [t for t in pool if t["season"] == args.season]
        if not pool:
            log.error("No tournaments matched — check --id or --season value")
            sys.exit(1)
        sample = fetch_standings(pool[0]["id"])
        print(f"\n{pool[0]['name']} — {len(sample)} standings")
        for s in sample[:5]:
            print(f"  #{s['placing']:3d} {s['player_name']:<25} {s['wins']}-{s['losses']}-{s['ties']}  {s['deck_name'] or '—'}")
        return

    # --- Ensure schema, then seed the tournament registry from the curated JSON.
    #     ON CONFLICT DO NOTHING preserves any manual corrections already in the DB. ---
    conn = get_conn()
    ensure_schema(conn)
    for t in load_curated():
        upsert_tournament(conn, t)
    conn.commit()
    conn.close()

    def _seed_discovered():
        """Scrape the Labs index and add any tournaments not yet in the registry."""
        try:
            discovered = discover_tournaments()
        except Exception as e:
            log.warning(f"Discovery failed (continuing with known tournaments): {e}")
            return
        c = get_conn()
        try:
            known = {r[0] for r in c.execute("SELECT labs_id FROM raw.major_tournaments").fetchall()}
            new = 0
            for t in discovered:
                if t["id"] not in known:
                    upsert_tournament(c, t)
                    new += 1
                    log.info(f"  discovered NEW: {t['id']} {t['date']} {t['tier']:<11} {t['name']}")
            c.commit()
        finally:
            c.close()
        log.info(f"Discovery: {len(discovered)} listed on Labs, {new} new added to registry")

    def _load_worklist():
        """Work-list comes from the DB (persists across deploys), newest-first."""
        c = get_conn()
        try:
            rows = c.execute("""
                SELECT labs_id, name, CAST(tournament_date AS VARCHAR), location, tier, season
                FROM raw.major_tournaments
                ORDER BY tournament_date DESC
            """).fetchall()
        finally:
            c.close()
        return [{"id": r[0], "name": r[1], "date": r[2],
                 "location": r[3], "tier": r[4], "season": r[5]} for r in rows]

    if args.discover:
        _seed_discovered()

    tournaments = _load_worklist()

    # If --id targets a tournament we don't know yet, discover once to resolve it.
    if args.id and not args.discover and not any(t["id"] == args.id for t in tournaments):
        _seed_discovered()
        tournaments = _load_worklist()

    # --- Filters ---
    if args.id:
        tournaments = [t for t in tournaments if t["id"] == args.id]
    elif args.season:
        tournaments = [t for t in tournaments if t["season"] == args.season]
    if args.current_rotation:
        cutoff = _rotation_cutoff()
        before = len(tournaments)
        tournaments = [t for t in tournaments if t["date"] >= cutoff]
        log.info(f"Current-rotation filter (>= {cutoff}): {len(tournaments)}/{before} tournaments")

    if not tournaments:
        log.error("No tournaments matched — check --id / --season / --current-rotation")
        sys.exit(1)

    log.info(f"Processing {len(tournaments)} tournament(s)...")

    for t in tournaments:
        # Open a fresh write connection per tournament so DuckDB is only locked
        # during the actual writes (~seconds), not the entire scrape duration.
        conn = get_conn()
        try:
            upsert_tournament(conn, t)
            log.info(f"  {t['id']} {t['name']}")
            standings = fetch_standings(t["id"])
            load_standings(conn, t["id"], standings)
            conn.commit()
            log.info(f"    {len(standings)} standings loaded")

            if not args.with_decklists:
                continue

            # Scrape players' matches + decklists
            if args.all_players:
                candidate_players = [s for s in standings if s["player_id"]]
                scope_label = "full field"
            else:
                candidate_players = [
                    s for s in standings
                    if s["player_id"] and s["placing"] and s["placing"] <= args.top_n
                ]
                scope_label = f"top {args.top_n}"

            already = set() if args.force else already_scraped_players(conn, t["id"])
            to_scrape = [p for p in candidate_players if p["player_id"] not in already]
        finally:
            conn.close()

        if not to_scrape:
            log.info(f"    {scope_label} already scraped ({len(already)} cached), skipping")
            continue

        log.info(f"    scraping {len(to_scrape)} players ({scope_label}, {len(already)} cached)...")

        # Scrape all HTTP (no DB open) then write in one short burst.
        scraped = []
        for p in to_scrape:
            pid  = p["player_id"]
            matches  = fetch_player_matches(t["id"], pid)
            decklist = fetch_player_decklist(t["id"], pid)
            scraped.append((p, matches, decklist))
            log.debug(f"      #{p['placing']:3d} {p['player_name']}: {len(matches)} rounds, {len(decklist)} cards")

        # Write burst — connection open only for the insert loop
        conn = get_conn()
        try:
            dl_count = match_count = 0
            for p, matches, decklist in scraped:
                load_player_matches(conn, t["id"], p["player_id"], matches)
                load_player_decklist(conn, t["id"], p["player_id"], decklist)
                if matches:  match_count += 1
                if decklist: dl_count   += 1
            conn.commit()
        finally:
            conn.close()

        log.info(f"    {match_count}/{len(to_scrape)} match pages, {dl_count}/{len(to_scrape)} decklists")


if __name__ == "__main__":
    main()
