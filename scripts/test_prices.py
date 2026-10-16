#!/usr/bin/env python3
"""
Tests for deck pricing: printing normalisation + mapping, the TCGCSV provider
(against a fake HTTP layer — no network), the snapshot writer,
SQL/Python card_key parity, list-cost math, budget-swap interval logic, the
affiliate redirect URL, and the polite scraper HTTP layer.

Run:  python3 scripts/test_prices.py      (or: pytest scripts/test_prices.py)
"""
import json
import os
import sys
import tempfile
from datetime import date

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ingest import prices as P  # noqa: E402
from ingest.http_polite import PoliteSession  # noqa: E402
from dashboard import pricing as X  # noqa: E402


# ---------------------------------------------------------------- normalisation
def test_norm_number():
    assert P.norm_number("085") == "85"
    assert P.norm_number("SVP085") == "85"
    assert P.norm_number("85/198") == "85"
    assert P.norm_number("TG05") == "TG5"
    assert P.norm_number("12a") == "12A"
    assert P.norm_number(7) == "7"
    assert P.norm_number("0") == "0"
    assert P.card_key("twm", "095") == "TWM-95"


def test_sql_card_key_matches_python():
    """The dbt macro must produce the same key as ingest/prices.py."""
    macro = open(os.path.join(ROOT, "transform/macros/card_key.sql")).read()
    body = macro.split("-%}", 1)[1].split("{%- endmacro %}")[0].strip()
    expr = body.replace("{{ set_col }}", "s").replace("{{ number_col }}", "n")
    cases = [("TWM", "095"), ("SVP", "SVP085"), ("svp", "85"), ("PAL", "185/193"),
             ("BRS", "TG05"), ("MEG", "12a"), ("MEE", "7")]
    c = duckdb.connect()
    for s, n in cases:
        got = c.execute(f"SELECT {expr} FROM (SELECT ? AS s, ? AS n)", (s, n)).fetchone()[0]
        assert got == P.card_key(s, n), (s, n, got, P.card_key(s, n))


def test_func_key_reprints():
    t1 = {"id": "a-1", "name": "Ultra Ball", "supertype": "Trainer"}
    t2 = {"id": "b-9", "name": "Ultra Ball", "supertype": "Trainer"}
    assert P.func_key(t1) == P.func_key(t2)
    atk = [{"name": "Phantom Dive", "damage": "200", "text": "Put 6 damage counters…", "cost": ["Fire", "Psychic"]}]
    p1 = {"id": "x-1", "name": "Dragapult ex", "supertype": "Pokémon", "hp": "320", "attacks": atk}
    p2 = {"id": "y-2", "name": "Dragapult ex", "supertype": "Pokémon", "hp": "320", "attacks": atk}
    p3 = {"id": "z-3", "name": "Dragapult ex", "supertype": "Pokémon", "hp": "310", "attacks": atk}
    assert P.func_key(p1) == P.func_key(p2), "identical text => reprint"
    assert P.func_key(p1) != P.func_key(p3), "different HP => different card"
    bare1 = {"id": "q-1", "name": "Pikachu", "supertype": "Pokémon"}
    bare2 = {"id": "q-2", "name": "Pikachu", "supertype": "Pokémon"}
    assert P.func_key(bare1) != P.func_key(bare2), "no text => never claimed as reprint"


# ---------------------------------------------------------------- tcgcsv provider
class FakeResp:
    def __init__(self, payload=None, text=None, status=200):
        self.status_code, self._p, self._t = status, payload, text

    def json(self):
        return self._p

    @property
    def text(self):
        return self._t

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _prod(pid, name, number, ctype="Trainer - Item", **ext):
    ed = [{"name": "Number", "value": number}, {"name": "Card Type", "value": ctype}]
    ed += [{"name": k.replace("_", " "), "value": v} for k, v in ext.items()]
    return {"productId": pid, "name": name, "extendedData": ed, "url": f"https://www.tcgplayer.com/product/{pid}"}


class FakeTcgcsv:
    """Serves the documented tcgcsv.com JSON shapes for 3 groups."""

    def __init__(self, build="2026-10-15T20:06:01+0000", price_mult=1.0):
        self.urls, self.build, self.mult = [], build, price_mult

    def get(self, url, immutable=False):
        self.urls.append(url)
        if url.endswith("/last-updated.txt"):
            return FakeResp(text=self.build)
        if url.endswith("/tcgplayer/3/groups"):
            return FakeResp({"results": [
                {"groupId": 1, "abbreviation": "TWM", "isSupplemental": False, "publishedOn": "2024-05-24"},
                {"groupId": 2, "abbreviation": "SVP", "isSupplemental": False, "publishedOn": "2023-01-01"},
                {"groupId": 3, "abbreviation": "ASC", "isSupplemental": False, "publishedOn": "2026-01-30"},
                {"groupId": 4, "abbreviation": "30C", "isSupplemental": True, "publishedOn": "2026-09-16"},
                {"groupId": 9, "abbreviation": "OLD", "isSupplemental": False, "publishedOn": "2001-01-01"}]})
        m = {"1": [_prod(10, "Dragapult ex", "130/167", "Psychic", HP="320", Attack_1="Phantom Dive (200)"),
                   _prod(11, "Secret Box", "163/167"),
                   _prod(12, "Twilight Masquerade Booster Box", "")],
             "2": [_prod(20, "Quaquaval - 005 (Prerelease)", "005", "Water", HP="160", Attack_1="Surf"),
                   _prod(21, "Quaquaval - 005", "005", "Water", HP="160", Attack_1="Surf")],
             "3": [_prod(30, "Secret Box", "199/217"),
                   _prod(31, "Dragapult ex", "160/217", "Psychic", HP="320", Attack_1="Phantom Dive (200)"),
                   _prod(32, "Boss's Orders [Ghetsis]", "183/217", "Supporter")],
             "9": [_prod(90, "Secret Box", "1/1")]}
        parts = url.rsplit("/", 2)
        gid, kind = parts[-2], parts[-1]
        if kind == "products":
            return FakeResp({"results": [x for x in m.get(gid, []) if x["extendedData"][0]["value"]]})
        if kind == "prices":
            px = {10: 7.5, 11: 1.1, 20: 4.0, 21: 2.0, 30: 0.3, 31: 5.0, 32: 0.2, 90: 0.01}
            out = []
            for prod in m.get(gid, []):
                if prod["productId"] in px:
                    v = round(px[prod["productId"]] * self.mult, 2)
                    out.append({"productId": prod["productId"], "marketPrice": v, "lowPrice": v * 0.8,
                                "subTypeName": "Normal"})
                    if prod["productId"] == 11:
                        out.append({"productId": 11, "marketPrice": 2.5, "lowPrice": 2.0, "subTypeName": "Reverse Holofoil"})
            return FakeResp({"results": out})
        return FakeResp({"results": []})


def _tcg(http, **kw):
    return P.TcgcsvProvider(http=http, overrides=kw.pop("overrides", {}), extra_groups_since="2020-01-01", **kw)


def test_clean_product_name():
    assert P.clean_product_name("Quaquaval - 005 (Prerelease)") == ("Quaquaval", ["Prerelease"])
    assert P.clean_product_name("Boss's Orders [Ghetsis]")[0] == "Boss's Orders"
    assert P.norm_name("Basic Fire Energy") == P.norm_name("Fire Energy")


def test_tcgcsv_maps_by_abbreviation_number_and_finds_reprints():
    http = FakeTcgcsv()
    prov = _tcg(http)
    todo = [P.Printing("TWM", "130", "Dragapult ex", 50), P.Printing("TWM", "163", "Secret Box", 10),
            P.Printing("SVP", "5", "Quaquaval", 3), P.Printing("TWM", "999", "Ghost Card", 1),
            P.Printing("ZZZ", "1", "Unknown Set Card", 1)]
    res = prov.fetch(todo, max_calls=50)
    maps = {m.card_key: m for m in res.mapping}
    assert maps["TWM-130"].provider_id == "10" and maps["TWM-163"].provider_id == "11"
    assert maps["SVP-5"].provider_id == "21", "plain promo preferred over the (Prerelease) stamp"
    assert maps["TWM-999"].status == "unmapped" and maps["ZZZ-1"].status == "unmapped"
    # reprints discovered in swept groups, keyed by that group's abbreviation
    assert maps["ASC-199"].func_key == maps["TWM-163"].func_key == "t:secret box"
    assert maps["ASC-160"].func_key == maps["TWM-130"].func_key, "same text => same Pokemon"
    assert "OLD-1" not in maps, "groups before TCGCSV_SWEEP_SINCE are not downloaded"
    assert not any("/9/" in u for u in http.urls)
    assert not any("/4/" in u for u in http.urls), "supplemental groups not swept"
    variants = {(r.card_key, r.variant): r.market_usd for r in res.prices}
    assert variants[("TWM-163", "Normal")] == 1.1 and variants[("TWM-163", "Reverse Holofoil")] == 2.5
    assert not any("tcgplayer.com" in u for u in http.urls), "never scrapes tcgplayer.com"


def test_tcgcsv_request_cap():
    http = FakeTcgcsv()
    prov = _tcg(http)
    prov.fetch([P.Printing("TWM", "130", "Dragapult ex", 1)], max_calls=3)
    assert len(http.urls) == 3     # last-updated + groups + products, then the cap stops it


def test_overrides_csv(tmp=None):
    d = tempfile.mkdtemp()
    f = os.path.join(d, "o.csv")
    with open(f, "w") as fh:
        fh.write("# comment\nset_code,number,tcgplayer_product_id,note\nPR-SV,085,31,x\nTWM,1,,skip\n")
    o = P.load_overrides(P.Path(f))
    assert o == {"PR-SV-85": 31, "TWM-1": None}
    http = FakeTcgcsv()
    res = _tcg(http, overrides=o).fetch([P.Printing("PR-SV", "085", "Dragapult ex", 1)], max_calls=50)
    m = [x for x in res.mapping if x.card_key == "PR-SV-85"]
    assert m and m[0].provider_id == "31" and m[0].reason == "manual override"


class Unavailable:
    name = "nokey"

    def available(self):
        return False


def test_unavailable_provider_is_a_noop():
    c = duckdb.connect()
    summary = P.run(c, Unavailable())
    assert summary["skipped"] == "no credentials"
    assert c.execute("SELECT COUNT(*) FROM raw.card_prices").fetchone()[0] == 0


def _seed_lists(c):
    c.execute("CREATE SCHEMA IF NOT EXISTS raw")
    c.execute("CREATE TABLE raw.tournaments (id VARCHAR, tournament_date TIMESTAMPTZ)")
    c.execute("CREATE TABLE raw.decklists (tournament_id VARCHAR, card_set VARCHAR, card_number VARCHAR, card_name VARCHAR, card_count INT)")
    c.execute("INSERT INTO raw.tournaments VALUES ('t1', now()), ('old', now() - INTERVAL 400 DAY)")
    c.execute("""INSERT INTO raw.decklists VALUES
        ('t1','TWM','130','Dragapult ex',2), ('t1','TWM','163','Secret Box',1),
        ('t1','TWM','0130','Dragapult ex',2), ('old','TWM','1','Rotated Card',4)""")


def test_run_keeps_daily_history_and_syncs_once_per_build():
    c = duckdb.connect()
    _seed_lists(c)
    work = P.recent_printings(c)
    assert [p.key for p in work] == ["TWM-130", "TWM-163"], "recent only, deduped by normalised key, most-played first"
    http = FakeTcgcsv()
    s1 = P.run(c, _tcg(http))
    n1 = c.execute("SELECT COUNT(*) FROM raw.card_prices").fetchone()[0]
    assert s1["unmapped"] == 0 and n1 > 0
    calls = len(http.urls)
    s2 = P.run(c, _tcg(http))
    assert s2["skipped"] == "already synced" and len(http.urls) == calls + 1, "only last-updated.txt re-checked"
    http2 = FakeTcgcsv(build="2026-10-16T20:05:00+0000", price_mult=2.0)
    P.run(c, _tcg(http2))
    hist = c.execute("""SELECT CAST(fetched_at AS DATE) d, market_usd FROM raw.card_prices
                        WHERE card_key='TWM-130' ORDER BY 1""").fetchall()
    assert [h[1] for h in hist] == [7.5, 15.0], "history kept, one snapshot per build day"


# ---------------------------------------------------------------- pricing math
def test_list_cost_counts_unpriced_never_guesses():
    lines = [{"name": "A", "count": 4}, {"name": "B", "count": 2}, {"name": "C", "count": 1}]
    prices = {"A": 1.5, "B": None, "C": 10.0}
    r = X.list_cost(lines, lambda ln: prices[ln["name"]])
    assert r == {"cost_usd": 16.0, "total_cards": 7, "priced_cards": 5, "unpriced_cards": 2, "is_lower_bound": True}


def test_median_iqr():
    r = X.median_iqr([10, 20, 30, 40, None])
    assert r == {"median": 25.0, "p25": 17.5, "p75": 32.5, "n": 4}
    assert X.median_iqr([]) is None


def test_shrinkage_pulls_small_samples_toward_zero():
    big = X.pooled_effect("x", 300, 200, 250, 250, 0.5)    # 60% vs 50%, n=500 each
    small = X.pooled_effect("y", 6, 4, 5, 5, 0.5)          # same raw rates, n=10
    assert 0.08 < big.delta < 0.10
    assert abs(small.delta) < 0.03, "Beta(15,15) prior dominates 10 matches"
    assert (small.hi - small.lo) > (big.hi - big.lo)


def test_interval_overlap():
    a = X.Effect("a", 0.02, 0.01, 100, 100)
    b = X.Effect("b", 0.03, 0.01, 100, 100)
    c = X.Effect("c", -0.08, 0.01, 100, 100)
    assert X.intervals_overlap(a, b) and not X.intervals_overlap(a, c)
    assert 0 < X.overlap_fraction(a, b) <= 1 and X.overlap_fraction(a, c) == 0


def test_suggest_swaps_reprint_first_and_honest_substitutes():
    cards = [
        {"card_name": "Pricey Ball", "card_category": "trainer", "median_listed_usd": 20.0, "avg_copies": 4,
         "inclusion_rate": 0.9, "expected_cost_usd": 72.0, "typical_card_key": "AAA-1",
         "cheapest_equiv_usd": 3.0, "cheapest_card_key": "BBB-7", "cheapest_variant": "normal"},
        {"card_name": "Cheap Ball", "card_category": "trainer", "median_listed_usd": 0.5, "avg_copies": 2,
         "inclusion_rate": 0.4, "expected_cost_usd": 0.4, "typical_card_key": "CCC-2",
         "cheapest_equiv_usd": 0.5, "cheapest_card_key": "CCC-2"},
        {"card_name": "Worse Ball", "card_category": "trainer", "median_listed_usd": 0.25, "avg_copies": 1,
         "inclusion_rate": 0.3, "expected_cost_usd": 0.1, "typical_card_key": "DDD-3",
         "cheapest_equiv_usd": 0.25, "cheapest_card_key": "DDD-3"},
        {"card_name": "Star ex", "card_category": "pokemon", "median_listed_usd": 30.0, "avg_copies": 2,
         "inclusion_rate": 1.0, "expected_cost_usd": 60.0, "typical_card_key": "EEE-1",
         "cheapest_equiv_usd": 30.0, "cheapest_card_key": "EEE-1"},
    ]
    eff = {"Pricey Ball": X.Effect("Pricey Ball", 0.01, 0.02, 200, 150),
           "Cheap Ball": X.Effect("Cheap Ball", 0.00, 0.02, 120, 230),
           "Worse Ball": X.Effect("Worse Ball", -0.15, 0.01, 90, 260),
           "Star ex": X.Effect("Star ex", 0.0, 0.05, 30, 30)}
    out = {s["card_name"]: s for s in X.suggest_swaps(cards, eff)}
    pb = out["Pricey Ball"]
    assert pb["reprint"]["card_key"] == "BBB-7" and pb["reprint"]["saves_per_copy"] == 17.0
    assert [s["card_name"] for s in pb["substitutes"]] == ["Cheap Ball"], "non-overlapping Worse Ball excluded"
    assert out["Star ex"]["substitutes"] == [] and "reprint" in out["Star ex"]["note"]
    assert list(out)[0] == "Pricey Ball", "ordered by $ contribution"


# ---------------------------------------------------------------- affiliate redirect
def test_tcgplayer_url_affiliate_and_plain():
    os.environ.pop("TCGPLAYER_AFFILIATE_URL", None)
    from dashboard import app as A
    plain = A.tcgplayer_url("Secret Box", "TWM", "163")
    assert plain.startswith("https://www.tcgplayer.com/search/") and "Secret+Box+TWM+163" in plain
    os.environ["TCGPLAYER_AFFILIATE_URL"] = "https://partner.tcgplayer.com/c/1/2/3"
    aff = A.tcgplayer_url("Secret Box")
    assert aff.startswith("https://partner.tcgplayer.com/c/1/2/3?u=https%3A%2F%2Fwww.tcgplayer.com")
    os.environ["TCGPLAYER_AFFILIATE_URL"] = "https://aff.example/x?dest={url}&s=1"
    assert A.tcgplayer_url("A").startswith("https://aff.example/x?dest=https%3A%2F%2F")
    os.environ.pop("TCGPLAYER_AFFILIATE_URL")
    client = A.app.test_client()
    r = client.get("/go/tcgplayer/Secret%20Box?set=TWM&n=163")
    assert r.status_code == 302 and "tcgplayer.com" in r.headers["Location"]
    assert r.headers["Cache-Control"] == "no-store"
    r = client.get("/go/tcgplayer/https:%2F%2Fevil.example")
    assert urlparse_host(r.headers["Location"]) == "www.tcgplayer.com", "never an open redirect"


def urlparse_host(u):
    from urllib.parse import urlparse
    return urlparse(u).hostname


# ---------------------------------------------------------------- polite HTTP
class FakeHTTP:
    def __init__(self, script):
        self.script, self.headers, self.seen = list(script), {}, []

    def get(self, url, headers=None, timeout=None):
        self.seen.append(dict(headers or {}))
        st, body, hd = self.script.pop(0)
        r = type("R", (), {})()
        r.status_code, r.content, r.headers = st, body, hd
        return r


def test_polite_session_cache_etag_backoff_and_throttle():
    d = tempfile.mkdtemp()
    sleeps = []
    clock = [0.0]
    fake = FakeHTTP([
        (503, b"", {"Retry-After": "3"}),
        (200, b"<html>v1</html>", {"ETag": '"abc"'}),
        (304, b"", {}),
    ])
    s = PoliteSession(cache_dir=d, session=fake, sleep=lambda x: (sleeps.append(x), clock.__setitem__(0, clock[0] + x)),
                      clock=lambda: clock[0], rng=lambda: 0.5)
    assert "indigocircuit.app" in s.session.headers["User-Agent"]
    r1 = s.get("https://labs.example/0076/standings")
    assert r1.status_code == 200 and r1.content == b"<html>v1</html>"
    assert 3.0 in sleeps, "honours Retry-After on 5xx"
    r2 = s.get("https://labs.example/0076/standings")
    assert fake.seen[-1].get("If-None-Match") == '"abc"' and r2.content == b"<html>v1</html>" and r2.from_cache
    n = len(fake.seen)
    r3 = s.get("https://labs.example/0076/standings", immutable=True)
    assert len(fake.seen) == n and r3.from_cache, "finished tournament => zero network"
    assert all(x >= 0 for x in sleeps) and any(abs(x - 1.0) < 1e-9 for x in sleeps), "~1 req/s throttle"


def test_labs_finished_and_decklist_fallback():
    from ingest import labs
    assert labs.is_finished("2026-10-01", today=date(2026, 10, 16))
    assert not labs.is_finished("2026-10-15", today=date(2026, 10, 16))
    html = ('<html><script>window.__x = {"pokemon":[{"name":"Dreepy","set":"TWM","number":128,"count":4}],'
            '"trainer":[{"name":"Ultra Ball","set":"MEG","number":131,"count":4}],"energy":[]};</script></html>')
    cards = labs.parse_decklist_html(html.encode())
    assert [(c["card_name"], c["card_count"]) for c in cards] == [("Dreepy", 4), ("Ultra Ball", 4)]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for f in fns:
        try:
            f()
            print(f"  [PASS] {f.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {f.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
