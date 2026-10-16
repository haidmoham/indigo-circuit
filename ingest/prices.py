"""
Card price ingest — pluggable providers -> raw.card_prices (daily snapshots).

Only printings that appear in *recent* decklists are priced (online lists from
the last PRICE_LOOKBACK_DAYS + current-rotation majors lists), most-played
first, with a hard cap on API calls per run, so a nightly run spends a small,
predictable number of credits. A printing already priced today is never
re-requested (re-runs are free).

Providers
---------
  tcgcsv   — https://tcgcsv.com, a free public daily mirror of TCGplayer's
             catalog (categories/groups/products) and market prices. Keyless.
             We follow its usage guidelines: identifiable User-Agent, >=100 ms
             between requests (we use ~250 ms), sync at most once per build
             (last-updated.txt is checked first), far below 10k requests/day.
             Only the Pokemon category (3) groups we need are downloaded:
             sets seen in recent decklists + main-line sets since
             TCGCSV_SWEEP_SINCE (for reprints). tcgplayer.com is never scraped.

Add another source by implementing PriceProvider and registering it in PROVIDERS.

The script always ensures the raw tables exist (so dbt models build even when
empty) and never exits non-zero on a provider failure — pricing is optional.

Tables
------
  raw.card_prices   DAILY PRICE HISTORY, one row per (printing, variant, day):
                    card_key, set_code, number, variant, market_usd, low_usd,
                    source, fetched_at, card_name, provider_id
  raw.card_id_map   Limitless (set_code, number) -> TCGplayer productId, plus a
                    functional-equivalence key (func_key) so reprints of the
                    *same card text* can be identified. status = mapped|unmapped.

Usage:
    python ingest/prices.py                 # price recent decklist printings
    python ingest/prices.py --max-calls 20  # tighter request cap
    python ingest/prices.py --dry-run       # show the work-list, spend nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Protocol

import duckdb

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    pass

log = logging.getLogger("prices")

DUCKDB_PATH = os.environ.get("DUCKDB_PATH", str(Path(__file__).parent.parent / "data" / "ptcg.duckdb"))
LOOKBACK_DAYS = int(os.environ.get("PRICE_LOOKBACK_DAYS", 60))
DEFAULT_MAX_CALLS = int(os.environ.get("PRICE_MAX_CALLS", 200))   # HTTP requests per run
DEFAULT_REPRINT_LOOKUPS = int(os.environ.get("PRICE_REPRINT_LOOKUPS", 0))


# ---------------------------------------------------------------------------
# Normalisation helpers (pure, unit-tested)
# ---------------------------------------------------------------------------

def norm_set(code: Optional[str]) -> str:
    return (code or "").strip().upper()


def norm_number(num) -> str:
    """'085' -> '85', 'SVP085' -> '85', '85/198' -> '85', 'TG05' -> 'TG5', '12a' -> '12A'."""
    s = str(num or "").strip().upper().split("/")[0]
    m = re.match(r"^([A-Z]*?)0*(\d+)([A-Z]?)$", s)
    if not m:
        return s
    prefix, digits, suffix = m.groups()
    # Promo-style prefixes that merely repeat the set code (SVP085, MEP012) are dropped;
    # gallery prefixes (TG, GG, SV for shiny vault) are meaningful and kept.
    if prefix in ("SVP", "MEP", "SWSH", "SM", "XY", "BW", "PR"):
        prefix = ""
    return f"{prefix}{digits}{suffix}"


def card_key(set_code: str, number) -> str:
    return f"{norm_set(set_code)}-{norm_number(number)}"


_NAME_WS = re.compile(r"\s+")


def norm_name(name: str) -> str:
    n = (name or "").replace("’", "'").replace("é", "e").replace("É", "E")
    n = _NAME_WS.sub(" ", n).strip().lower()
    # 'Basic Fire Energy' (TCGplayer) == 'Fire Energy' (Limitless)
    return n[6:] if n.startswith("basic ") and n.endswith(" energy") else n


def func_key(card: dict) -> str:
    """Functional-equivalence key. Two printings with the same func_key are the
    same card for deck-building purposes (a 'reprint').

    Trainers / Energy: the name is the card.
    Pokémon: same name is NOT enough (different attacks per set), so we hash
    name + HP + abilities + attacks text. Missing text => unique key (never
    claimed as a reprint)."""
    name = norm_name(card.get("name", ""))
    supertype = (card.get("supertype") or "").lower()
    if supertype.startswith("trainer") or supertype.startswith("energy"):
        return f"t:{name}"
    abil = card.get("abilities") or []
    atk = card.get("attacks") or []
    if not abil and not atk:
        return f"p:{name}:{card.get('id', '')}"
    blob = json.dumps({
        "hp": str(card.get("hp") or ""),
        "ab": [(norm_name(a.get("name", "")), norm_name(a.get("text", ""))) for a in abil],
        "at": [(norm_name(a.get("name", "")), str(a.get("damage") or ""),
                norm_name(a.get("text", "")), sorted(a.get("cost") or [])) for a in atk],
    }, sort_keys=True)
    return f"p:{name}:{hashlib.sha1(blob.encode()).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Printing:
    set_code: str
    number: str
    card_name: str
    plays: int = 0

    @property
    def key(self) -> str:
        return card_key(self.set_code, self.number)


@dataclass
class PriceRow:
    card_key: str
    set_code: str
    number: str
    variant: str
    market_usd: Optional[float]
    low_usd: Optional[float]
    source: str
    card_name: str
    provider_id: str


@dataclass
class MapRow:
    card_key: str
    set_code: str
    number: str
    provider: str
    provider_id: Optional[str]
    card_name: Optional[str]
    supertype: Optional[str]
    func_key: Optional[str]
    status: str               # mapped | unmapped
    reason: Optional[str] = None


@dataclass
class FetchResult:
    prices: list[PriceRow] = field(default_factory=list)
    mapping: list[MapRow] = field(default_factory=list)
    calls: int = 0


class PriceProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def fetch(self, printings: list[Printing], max_calls: int) -> FetchResult: ...

    def lookup_reprints(self, names: list[str], max_calls: int) -> FetchResult: ...


class BudgetExceeded(Exception):
    pass


# ---------------------------------------------------------------------------
# TCGCSV (free daily mirror of TCGplayer's catalog + market prices)
# ---------------------------------------------------------------------------
# https://tcgcsv.com — docs ask for: a custom User-Agent, >=100 ms between
# requests, at most one sync per day (check last-updated.txt first), and
# < 10,000 requests/day. robots.txt allows all. We do ~2 requests per needed
# set (products + prices), typically < 80 per night.

OVERRIDES_CSV = Path(__file__).parent.parent / "data" / "card_id_overrides.csv"

# Limitless set codes whose TCGplayer group abbreviation differs (or is ambiguous).
LIMITLESS_TO_TCGPLAYER_GROUP = {
    "PR-SV": 22872,   # SV promos (Limitless sometimes uses PR-SV)
    "SVP":   22872,
    "MEP":   24451,
    "30C":   24722,   # not the supplemental "Classic Collection" group
}

_PAREN = re.compile(r"\s*\(([^)]*)\)\s*")
_NUM_SUFFIX = re.compile(r"\s+-\s+[A-Z]*\d+[A-Za-z]?(/\d+)?$")


def clean_product_name(name: str) -> tuple[str, list[str]]:
    """'Quaquaval - 005 (Prerelease)' -> ('Quaquaval', ['Prerelease'])."""
    tags = _PAREN.findall(name or "")
    base = _PAREN.sub(" ", name or "").strip()
    base = _NUM_SUFFIX.sub("", base).strip()
    # 'Boss's Orders [Ghetsis]' — bracketed art/character labels are not part of the card name
    base = re.sub(r"\s*\[[^\]]*\]", "", base).strip()
    return base, tags


def _ext(product: dict) -> dict:
    return {e.get("name"): e.get("value") for e in product.get("extendedData") or []}


def tcg_card_dict(product: dict) -> Optional[dict]:
    """TCGplayer product -> the card-ish dict func_key() understands. None for sealed."""
    ext = _ext(product)
    if not ext.get("Number"):
        return None
    name, tags = clean_product_name(product.get("name", ""))
    ctype = (ext.get("Card Type") or "")
    ct = ctype.lower()
    if "energy" in ct:
        supertype = "Energy"
    elif ext.get("HP") or any(k.startswith("Attack") for k in ext):
        supertype = "Pokémon"
    else:   # 'Trainer - Item', 'Supporter', 'Stadium', 'Pokemon Tool', ...
        supertype = "Trainer"
    attacks = [{"name": ext[k], "text": "", "damage": "", "cost": []}
               for k in sorted(ext) if k.startswith("Attack")]
    abilities = [{"name": "text", "text": ext["CardText"]}] if ext.get("CardText") and supertype == "Pokémon" else []
    return {"id": str(product.get("productId")), "name": name, "supertype": supertype,
            "hp": ext.get("HP"), "attacks": attacks, "abilities": abilities,
            "number": ext.get("Number"), "tags": tags, "url": product.get("url")}


def load_overrides(path: Path = OVERRIDES_CSV) -> dict[str, Optional[int]]:
    """card_key -> productId (or None = deliberately unpriceable)."""
    import csv
    out: dict[str, Optional[int]] = {}
    if not path.exists():
        return out
    with open(path, newline="") as f:
        for row in csv.DictReader(r for r in f if not r.lstrip().startswith("#")):
            k = card_key(row["set_code"], row["number"])
            pid = (row.get("tcgplayer_product_id") or "").strip()
            out[k] = int(pid) if pid.isdigit() else None
    return out


class TcgcsvProvider:
    name = "tcgcsv"
    BASE = "https://tcgcsv.com"
    CATEGORY = 3   # Pokemon

    def __init__(self, http=None, overrides: Optional[dict] = None,
                 extra_groups_since: Optional[str] = None):
        self._http = http
        self.overrides = load_overrides() if overrides is None else overrides
        # Also sweep every main-line group released since this date so reprints
        # in sets nobody currently registers are still found.
        self.extra_groups_since = extra_groups_since or os.environ.get("TCGCSV_SWEEP_SINCE", "2023-03-01")
        self.calls = 0
        self.max_calls = 0
        self._groups: Optional[list[dict]] = None
        self.build_ts: Optional[datetime] = None

    def available(self) -> bool:
        return True   # public, keyless

    @property
    def http(self):
        if self._http is None:
            try:
                from ingest.http_polite import PoliteSession
            except ImportError:
                from http_polite import PoliteSession
            cache = os.environ.get("HTTP_CACHE_DIR") or str(Path(DUCKDB_PATH).parent / "http_cache")
            self._http = PoliteSession(cache_dir=str(Path(cache) / "tcgcsv"), min_interval=0.25, jitter=0.3)
            self._http.session.headers["Accept"] = "application/json, text/plain"
        return self._http

    def _get(self, path: str, cacheable: bool = True):
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"request cap {self.max_calls} reached")
        self.calls += 1
        r = self.http.get(f"{self.BASE}{path}")
        r.raise_for_status()
        return r

    def last_updated(self) -> datetime:
        txt = self._get("/last-updated.txt").text.strip()
        self.build_ts = datetime.strptime(txt, "%Y-%m-%dT%H:%M:%S%z")
        return self.build_ts

    def groups(self) -> list[dict]:
        if self._groups is None:
            self._groups = self._get(f"/tcgplayer/{self.CATEGORY}/groups").json()["results"]
        return self._groups

    def group_for(self, set_code: str) -> Optional[int]:
        sc = norm_set(set_code)
        if sc in LIMITLESS_TO_TCGPLAYER_GROUP:
            return LIMITLESS_TO_TCGPLAYER_GROUP[sc]
        cands = [g for g in self.groups() if norm_set(g.get("abbreviation")) == sc]
        if not cands:
            return None
        cands.sort(key=lambda g: (g.get("isSupplemental", False), g.get("publishedOn", "")))
        return cands[0]["groupId"]

    def _group_data(self, gid: int) -> tuple[list[dict], dict[int, list[dict]]]:
        products = self._get(f"/tcgplayer/{self.CATEGORY}/{gid}/products").json()["results"]
        prices = self._get(f"/tcgplayer/{self.CATEGORY}/{gid}/prices").json()["results"]
        by_pid: dict[int, list[dict]] = {}
        for p in prices:
            by_pid.setdefault(p["productId"], []).append(p)
        return products, by_pid

    @staticmethod
    def _variants(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows or []:
            mk, lo = r.get("marketPrice"), r.get("lowPrice")
            if mk is None and lo is None:
                continue
            out.append({"variant": r.get("subTypeName") or "Normal",
                        "market": float(mk) if mk is not None else None,
                        "low": float(lo) if lo is not None else None})
        return out

    def fetch(self, printings: list[Printing], max_calls: int) -> FetchResult:
        self.max_calls = self.calls + max_calls
        res = FetchResult()
        try:
            if self.build_ts is None:
                self.last_updated()
            want_by_group: dict[int, list[Printing]] = {}
            group_code: dict[int, str] = {}
            for p in printings:
                gid = self.group_for(p.set_code)
                if gid is None and p.key not in self.overrides:
                    res.mapping.append(MapRow(p.key, p.set_code, p.number, self.name, None, p.card_name,
                                              None, None, "unmapped",
                                              f"no TCGplayer group with abbreviation {p.set_code!r}"))
                    continue
                if gid is not None:
                    want_by_group.setdefault(gid, []).append(p)
                    group_code[gid] = p.set_code
            names = {norm_name(p.card_name) for p in printings}
            sweep = [g["groupId"] for g in self.groups()
                     if not g.get("isSupplemental") and (g.get("publishedOn") or "") >= self.extra_groups_since]
            abbrev = {g["groupId"]: norm_set(g.get("abbreviation") or f"G{g['groupId']}") for g in self.groups()}
            order = sorted(want_by_group, key=lambda g: -sum(p.plays for p in want_by_group[g]))
            order += [g for g in sweep if g not in want_by_group]
            override_pids = {pid: k for k, pid in self.overrides.items() if pid}
            seen_pid: set[int] = set()
            for gid in order:
                try:
                    products, prices = self._group_data(gid)
                except BudgetExceeded:
                    raise
                except Exception as e:  # one bad group never sinks the run
                    log.warning(f"tcgcsv group {gid}: {e}")
                    continue
                code = group_code.get(gid) or abbrev.get(gid, f"G{gid}")
                # index products by normalised number; prefer un-tagged (no '(Prerelease)' etc.)
                by_num: dict[str, list[dict]] = {}
                for prod in products:
                    cd = tcg_card_dict(prod)
                    if not cd:
                        continue
                    cd["_pid"] = prod["productId"]
                    by_num.setdefault(norm_number(cd["number"]), []).append(cd)
                for p in want_by_group.get(gid, []):
                    if p.key in self.overrides:
                        continue   # handled via override_pids below
                    cands = by_num.get(norm_number(p.number), [])
                    if len(cands) > 1:
                        same = [c for c in cands if norm_name(c["name"]) == norm_name(p.card_name)]
                        cands = same or cands
                        cands.sort(key=lambda c: (len(c["tags"]) > 0, c["_pid"]))
                    if not cands:
                        res.mapping.append(MapRow(p.key, p.set_code, p.number, self.name, None, p.card_name,
                                                  None, None, "unmapped", f"number not found in group {gid}"))
                        continue
                    self._emit(res, cands[0], p.key, p.set_code, p.number, prices, seen_pid,
                               note=(f"name differs: {cands[0]['name']!r}"
                                     if norm_name(cands[0]["name"]) != norm_name(p.card_name) else None))
                # reprints / same-name printings anywhere in the swept groups
                for num, cds in by_num.items():
                    for cd in cds:
                        if cd["_pid"] in override_pids:
                            k = override_pids[cd["_pid"]]
                            st, nb = k.split("-", 1)
                            self._emit(res, cd, k, st, nb, prices, seen_pid, note="manual override")
                        elif norm_name(cd["name"]) in names and not cd["tags"]:
                            self._emit(res, cd, card_key(code, num), code, num, prices, seen_pid)
        except BudgetExceeded as e:
            log.warning(f"tcgcsv request cap: {e}; remaining groups deferred to next run")
        res.calls = self.calls
        return res

    def _emit(self, res: FetchResult, cd: dict, key: str, set_code: str, number: str,
              prices: dict, seen_pid: set, note: Optional[str] = None):
        pid = cd["_pid"]
        if (pid, key) in seen_pid:
            return
        seen_pid.add((pid, key))
        variants = self._variants(prices.get(pid))
        res.mapping.append(MapRow(key, norm_set(set_code), norm_number(number), self.name, str(pid),
                                  cd["name"], cd["supertype"], func_key(cd), "mapped",
                                  note or (None if variants else "mapped but no market price")))
        for v in variants:
            res.prices.append(PriceRow(key, norm_set(set_code), norm_number(number), v["variant"],
                                       v["market"], v["low"], self.name, cd["name"], str(pid)))

    def lookup_reprints(self, names: list[str], max_calls: int) -> FetchResult:
        return FetchResult()   # the group sweep in fetch() already covers reprints


PROVIDERS = {"tcgcsv": TcgcsvProvider}


def get_provider(name: Optional[str] = None) -> PriceProvider:
    name = (name or os.environ.get("PRICE_PROVIDER") or "tcgcsv").lower()
    return PROVIDERS[name]()


# ---------------------------------------------------------------------------
# DuckDB
# ---------------------------------------------------------------------------

def ensure_schema(conn):
    conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.card_prices (
            card_key     VARCHAR NOT NULL,
            set_code     VARCHAR NOT NULL,
            number       VARCHAR NOT NULL,
            variant      VARCHAR NOT NULL,
            market_usd   DOUBLE,
            low_usd      DOUBLE,
            source       VARCHAR NOT NULL,
            fetched_at   TIMESTAMPTZ NOT NULL,
            card_name    VARCHAR,
            provider_id  VARCHAR
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw.card_id_map (
            card_key     VARCHAR NOT NULL,
            set_code     VARCHAR NOT NULL,
            number       VARCHAR NOT NULL,
            provider     VARCHAR NOT NULL,
            provider_id  VARCHAR,
            card_name    VARCHAR,
            supertype    VARCHAR,
            func_key     VARCHAR,
            status       VARCHAR NOT NULL,
            reason       VARCHAR,
            updated_at   TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (card_key, provider)
        )""")


def _table_exists(conn, schema, table) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?",
        (schema, table)).fetchone()[0] > 0


def recent_printings(conn, lookback_days: int = LOOKBACK_DAYS, today=None) -> list[Printing]:
    """Unique (set, number) printings in recent lists, most-played first."""
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=lookback_days)).isoformat()
    parts = []
    params: list = []
    if _table_exists(conn, "raw", "decklists") and _table_exists(conn, "raw", "tournaments"):
        parts.append("""
            SELECT d.card_set AS set_code, d.card_number AS number, d.card_name, d.card_count
            FROM raw.decklists d JOIN raw.tournaments t ON t.id = d.tournament_id
            WHERE t.tournament_date >= ?::DATE""")
        params.append(since)
    if _table_exists(conn, "raw", "major_decklists") and _table_exists(conn, "raw", "major_tournaments"):
        parts.append("""
            SELECT d.card_set, d.card_number, d.card_name, d.card_count
            FROM raw.major_decklists d JOIN raw.major_tournaments t ON t.labs_id = d.labs_tournament_id
            WHERE t.tournament_date >= ?::DATE""")
        params.append(since)
    if not parts:
        return []
    rows = conn.execute(f"""
        SELECT set_code, number, MAX(card_name), SUM(COALESCE(card_count, 1)) AS plays
        FROM ({' UNION ALL '.join(parts)})
        WHERE set_code IS NOT NULL AND number IS NOT NULL AND card_name IS NOT NULL
        GROUP BY set_code, number ORDER BY plays DESC""", params).fetchall()
    seen, out = set(), []
    for s, n, name, plays in rows:
        p = Printing(norm_set(s), norm_number(n), name, int(plays or 0))
        if p.key not in seen:
            seen.add(p.key)
            out.append(p)
    return out


def priced_today(conn, source: str, today=None) -> set[str]:
    today = today or datetime.now(timezone.utc).date()
    rows = conn.execute("""
        SELECT DISTINCT card_key FROM raw.card_prices
        WHERE source = ? AND CAST(fetched_at AS DATE) = ?::DATE""", (source, today.isoformat())).fetchall()
    done = {r[0] for r in rows}
    # Printings already known-unmapped today are not retried either.
    rows = conn.execute("""
        SELECT card_key FROM raw.card_id_map
        WHERE provider = ? AND CAST(updated_at AS DATE) = ?::DATE""", (source, today.isoformat())).fetchall()
    return done | {r[0] for r in rows}


def write_result(conn, res: FetchResult, now=None):
    """Append one snapshot per printing per source per day (history is kept;
    re-running the same day replaces that day's rows only)."""
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    keys = sorted({(p.card_key, p.source) for p in res.prices})
    for k, src in keys:   # one snapshot per printing per source per day
        conn.execute("DELETE FROM raw.card_prices WHERE card_key=? AND source=? AND CAST(fetched_at AS DATE)=?::DATE",
                     (k, src, day))
    if res.prices:
        conn.executemany("INSERT INTO raw.card_prices VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (p.card_key, p.set_code, p.number, p.variant, p.market_usd, p.low_usd, p.source, now,
             p.card_name, p.provider_id) for p in res.prices])
    for m in res.mapping:
        conn.execute("""
            INSERT INTO raw.card_id_map VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (card_key, provider) DO UPDATE SET
              provider_id = COALESCE(excluded.provider_id, card_id_map.provider_id),
              card_name   = COALESCE(excluded.card_name, card_id_map.card_name),
              supertype   = COALESCE(excluded.supertype, card_id_map.supertype),
              func_key    = COALESCE(excluded.func_key, card_id_map.func_key),
              status      = CASE WHEN card_id_map.status = 'mapped' AND excluded.status = 'unmapped'
                                 THEN card_id_map.status ELSE excluded.status END,
              reason      = excluded.reason,
              updated_at  = excluded.updated_at""",
            (m.card_key, m.set_code, m.number, m.provider, m.provider_id, m.card_name, m.supertype,
             m.func_key, m.status, m.reason, now))


def expensive_names(conn, source: str, limit: int) -> list[str]:
    rows = conn.execute("""
        SELECT card_name, MAX(market_usd) mx FROM raw.card_prices
        WHERE source = ? AND market_usd IS NOT NULL
        GROUP BY card_name ORDER BY mx DESC LIMIT ?""", (source, limit)).fetchall()
    return [r[0] for r in rows if r[0]]


def run(conn, provider: PriceProvider, max_calls: int = DEFAULT_MAX_CALLS,
        reprint_lookups: int = DEFAULT_REPRINT_LOOKUPS, dry_run: bool = False) -> dict:
    ensure_schema(conn)
    work = recent_printings(conn)
    summary = {"provider": provider.name, "recent_printings": len(work)}
    if dry_run:
        log.info(f"dry run: {summary}; first 10: {[p.key for p in work[:10]]}")
        return summary
    if not provider.available():
        log.info(f"price provider '{provider.name}' unavailable (no credentials) — skipping. {summary}")
        summary["skipped"] = "no credentials"
        return summary
    snap = None
    if hasattr(provider, "last_updated"):
        provider.max_calls = provider.calls + 1
        snap = provider.last_updated()          # 1 tiny request
        have = conn.execute("SELECT MAX(fetched_at) FROM raw.card_prices WHERE source = ?",
                            (provider.name,)).fetchone()[0]
        if have is not None and have >= snap:
            log.info(f"{provider.name}: snapshot {snap.isoformat()} already stored — nothing to do")
            summary["skipped"] = "already synced"
            return summary
        todo = work
    else:
        done = priced_today(conn, provider.name)
        todo = [p for p in work if p.key not in done]
    summary["todo"] = len(todo)
    res = provider.fetch(todo, max_calls=max_calls) if todo else FetchResult()
    write_result(conn, res, now=snap)
    spent = res.calls
    if reprint_lookups and spent < max_calls:
        names = expensive_names(conn, provider.name, reprint_lookups)
        rres = provider.lookup_reprints(names, max_calls=min(reprint_lookups, max_calls - spent))
        write_result(conn, rres, now=snap)
        spent = rres.calls or spent
    unmapped = [m for m in res.mapping if m.status == "unmapped"]
    for m in unmapped[:50]:
        log.warning(f"  unmapped {m.card_key} ({m.card_name}): {m.reason}")
    if len(unmapped) > 50:
        log.warning(f"  ... {len(unmapped) - 50} more unmapped (see raw.card_id_map where status='unmapped')")
    summary.update(calls=spent, price_rows=len(res.prices),
                   mapped=sum(1 for m in res.mapping if m.status == "mapped"), unmapped=len(unmapped),
                   snapshot=snap.isoformat() if snap else None)
    log.info(f"prices: {summary}")
    return summary


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    ap.add_argument("--reprint-lookups", type=int, default=DEFAULT_REPRINT_LOOKUPS)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = duckdb.connect(DUCKDB_PATH)
    try:
        ensure_schema(conn)
        conn.commit()
        try:
            run(conn, get_provider(a.provider), a.max_calls, a.reprint_lookups, a.dry_run)
            conn.commit()
        except Exception as e:
            # Pricing is optional: a provider outage must never block the nightly
            # pipeline. Yesterday's snapshot stays in raw.card_prices.
            log.error(f"price ingest failed (continuing without new prices): {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
