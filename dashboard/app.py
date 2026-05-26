"""
Indigo Circuit — Flask dashboard.
Reads from DuckDB via DUCKDB_PATH environment variable.
"""
import json
import os
import re
import shutil
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote as urlquote

import duckdb
import requests as http
from flask import Flask, render_template, request, jsonify, redirect
from flask_compress import Compress
from dotenv import load_dotenv

import logging
load_dotenv()

app = Flask(__name__)
Compress(app)   # gzip all JSON/HTML responses >500 bytes automatically

# Ensure pipeline INFO logs are visible in Railway (gunicorn suppresses them by default)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app.logger.setLevel(logging.INFO)

# Schema where dbt marts live (override with env var in production)
MARTS = os.environ.get("DBT_MARTS_SCHEMA", "dbt_dev_marts")
DUCKDB_PATH = os.environ.get("DUCKDB_PATH", str(Path(__file__).parent.parent / "data" / "ptcg.duckdb"))


# ---------------------------------------------------------------------------
# DuckDB query helper — open a fresh read-only connection per query.
# A cross-process signal file (_PIPELINE_SIGNAL) blocks all reads while
# the pipeline holds an exclusive write lock. Workers see the file and
# return [] immediately, guaranteeing no reader/writer contention.
# ---------------------------------------------------------------------------
_PIPELINE_SIGNAL = str(Path(DUCKDB_PATH).parent / "pipeline_writing")


def query(sql, params=None):
    """Execute SQL and return list-of-dicts with UPPERCASE keys.
    Returns [] if the database or schema isn't ready yet."""
    if not os.path.exists(DUCKDB_PATH):
        return []
    if os.path.exists(_PIPELINE_SIGNAL):
        return []   # pipeline is writing — serve from cache or empty
    try:
        conn = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            cur = conn.execute(sql, params or [])
            cols = [d[0].upper() for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
    except (duckdb.CatalogException, duckdb.IOException):
        return []
    except Exception:
        raise


# ---------------------------------------------------------------------------
# TTL result cache
# Data changes at most once a day (overnight ingests), so 5-min cache makes
# repeat page loads near-instant while staying fresh enough for live use.
# ---------------------------------------------------------------------------
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL = int(os.environ.get("CACHE_TTL", 300))   # default 5 minutes


def cached(key: str, fn, ttl: int = CACHE_TTL):
    """Return cached value for *key*, or call *fn()* and cache the result."""
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and now - entry["ts"] < ttl:
            return entry["v"]
    result = fn()
    with _CACHE_LOCK:
        _CACHE[key] = {"v": result, "ts": time.monotonic()}
    return result


def cached_json(key: str, fn, ttl: int = CACHE_TTL):
    """Like cached() but returns a Flask Response with gzip-friendly Cache-Control."""
    from flask import make_response
    data = cached(key, fn, ttl)
    resp = make_response(jsonify(data))
    resp.headers["Cache-Control"] = f"public, max-age={ttl}"
    return resp
    return result


def bust_cache(*keys):
    """Invalidate specific cache keys (or all if none given)."""
    with _CACHE_LOCK:
        if keys:
            for k in keys:
                _CACHE.pop(k, None)
        else:
            _CACHE.clear()


@app.after_request
def add_cache_headers(response):
    """Add Cache-Control to API responses so Cloudflare/browser can cache them."""
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", f"public, max-age={CACHE_TTL}")
    return response


# ---------------------------------------------------------------------------
# Limitless player-ID resolver
# ---------------------------------------------------------------------------
# Labs-scrape player_ids don't map to limitlesstcg.com IDs, so we resolve
# them on-demand by taking the first result from the Limitless player search.
# Results are cached in Snowflake so each name is only fetched once.
# To correct a bad mapping: UPDATE RAW.LIMITLESS_PLAYER_IDS
#   SET limitless_id = '<correct_id>' WHERE player_name_lower = '<name>';

_lid_cache: dict = {}          # L1 — in-process, survives within one worker
_lid_table_ready: bool = False


def _ensure_lid_table():
    # Read-only connection — skip table creation; rely on in-memory cache only
    global _lid_table_ready
    _lid_table_ready = True


def _resolve_limitless_id(name: str):
    """Return the limitlesstcg.com player ID for *name*, or None on failure."""
    _ensure_lid_table()
    key = name.lower().strip()

    if key in _lid_cache:
        return _lid_cache[key]

    # No persistent cache (read-only DB); in-memory L1 cache above is sufficient


    # Live fetch — first result of limitlesstcg.com player search
    lid = None
    try:
        r = http.get(
            "https://limitlesstcg.com/players",
            params={"q": name},
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        m = re.search(r'href="/players/(\d+)"', r.text)
        if m:
            lid = m.group(1)
    except Exception:
        pass

    _lid_cache[key] = lid

    # In-memory only — no write-back (read-only DB connection)

    return lid


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("league.html")

@app.get("/search")
def search():
    return render_template("index.html")

@app.get("/player/<path:name>")
def player_report(name):
    return render_template("player.html", player_name=name)

@app.get("/meta")
def meta():
    return render_template("meta.html")

@app.get("/league")
def league():
    # keep old URL working
    return render_template("league.html")

@app.get("/leaderboard")
def leaderboard():
    return render_template("leaderboard.html")

@app.get("/online")
def online():
    return render_template("online.html")

@app.get("/tech")
def tech():
    return render_template("tech.html")


@app.get("/ev-lab")
def ev_lab():
    return render_template("ev_lab.html")


@app.post("/api/ev/compute")
def ev_compute():
    """
    Compute Bayesian meta-weighted EV for a submitted decklist.
    Body: {"decklist": "<ptcg live export>", "archetype_id": "<optional override>"}
    """
    try:
        body         = request.get_json(force=True) or {}
        raw_list     = body.get("decklist", "").strip()
        archetype_id = body.get("archetype_id") or None
        source       = body.get("source", "online")
        if source not in ("online", "majors", "both"):
            source = "online"
        if not raw_list:
            return jsonify({"error": "decklist is required"}), 400

        from .ev import compute_list_ev
        conn   = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            result = compute_list_ev(raw_list, conn, archetype_id=archetype_id, source=source)
        finally:
            conn.close()

        if "error" in result:
            return jsonify(result), 422
        return jsonify(result)
    except duckdb.CatalogException:
        return jsonify({"error": "EV data is still being built — check back after the nightly pipeline runs."}), 503
    except duckdb.IOException:
        app.logger.warning("[ev] DuckDB locked — pipeline likely running")
        return jsonify({"error": "Data is temporarily unavailable while the pipeline runs — try again in a moment."}), 503
    except Exception as e:
        app.logger.exception("[ev] compute error")
        return jsonify({"error": "An unexpected error occurred — check server logs."}), 500


@app.get("/api/ev/archetypes")
def ev_archetypes():
    """List of archetypes available for manual override in EV Lab."""
    source = request.args.get("source", "online")
    if source not in ("online", "majors", "both"):
        source = "online"

    if source == "both":
        rows = query(f"""
            SELECT ca.deck_id,
                   MIN(COALESCE(s.deck_name, ms.deck_name)) AS deck_name,
                   ca.total_lists
            FROM (
                SELECT deck_id, total_lists FROM {MARTS}.card_archetype_stats
                UNION
                SELECT deck_id, total_lists FROM {MARTS}.major_card_archetype_stats
            ) ca
            LEFT JOIN raw.standings s
                   ON s.deck_id = ca.deck_id AND s.deck_name IS NOT NULL
            LEFT JOIN raw.major_standings ms
                   ON ms.deck_id = ca.deck_id AND ms.deck_name IS NOT NULL
            GROUP BY ca.deck_id, ca.total_lists
            ORDER BY ca.total_lists DESC
        """)
    elif source == "majors":
        rows = query(f"""
            SELECT ca.deck_id,
                   MIN(s.deck_name) AS deck_name,
                   ca.total_lists
            FROM {MARTS}.major_card_archetype_stats ca
            LEFT JOIN raw.major_standings s
                   ON s.deck_id = ca.deck_id AND s.deck_name IS NOT NULL
            GROUP BY ca.deck_id, ca.total_lists
            ORDER BY ca.total_lists DESC
        """)
    else:
        rows = query(f"""
            SELECT ca.deck_id,
                   MIN(s.deck_name) AS deck_name,
                   ca.total_lists
            FROM {MARTS}.card_archetype_stats ca
            LEFT JOIN raw.standings s
                   ON s.deck_id = ca.deck_id AND s.deck_name IS NOT NULL
            GROUP BY ca.deck_id, ca.total_lists
            ORDER BY ca.total_lists DESC
        """)
    return jsonify([
        {"deck_id": r["DECK_ID"], "deck_name": r["DECK_NAME"], "total_lists": r["TOTAL_LISTS"]}
        for r in rows
    ])

@app.get("/demons")
def demons():
    return render_template("demons.html")

@app.get("/go/<path:name>")
def limitless_go(name):
    """Resolve player name → limitlesstcg.com profile via search first-result."""
    lid = _resolve_limitless_id(name)
    if lid:
        return redirect(f"https://limitlesstcg.com/players/{lid}", 302)
    return redirect(f"https://limitlesstcg.com/players?q={urlquote(name)}", 302)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/search")
def search_players():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    # Majors players only — online players have no profile pages on Limitless
    rows = query(
        f"""
        SELECT
            lower(player_name)              AS player_username,
            max(player_name)                AS player_name,
            max(country)                    AS country,
            count(distinct tournament_id)   AS tournaments_entered,
            min("placing")                    AS best_placement,
            -- most recent player_id as canonical Limitless profile link
            max_by(player_id, tournament_date) AS player_id
        FROM {MARTS}.MAJOR_PLAYER_HISTORY
        WHERE lower(player_name) LIKE lower(?)
          AND player_id IS NOT NULL
          AND tournament_date >= current_date - INTERVAL '52 weeks'
        GROUP BY lower(player_name)
        ORDER BY count(distinct tournament_id) DESC
        LIMIT 20
        """,
        (f"%{q}%",),
    )
    return jsonify(rows)


@app.get("/api/player/<path:name>")
def player_stats(name):
    # Seasonal ranking row (ATP score, title, gym leader, etc.)
    ranking = query(
        f"""
        SELECT sr.rank, sr.player_name, sr.player_id, sr.country,
               sr.majors_counted, sr.win_rate_pct, sr.best_placing,
               sr.top8s, sr.top16s, sr.atp_score, sr.title,
               sr.top_deck_name, sr.top_deck_sprite,
               sr.worlds_top8s, sr.ic_top8s, sr.regional_top8s,
               sr.last_played, sr.worlds_score, sr.ic_score, sr.regional_score,
               mgl.archetype AS gym_leader_of
        FROM {MARTS}.SEASONAL_RANKINGS sr
        LEFT JOIN {MARTS}.MAJOR_GYM_LEADERS mgl
          ON lower(sr.player_name) = lower(mgl.player_name)
          AND mgl.is_gym_leader = TRUE
        WHERE lower(sr.player_name) = lower(?)
        LIMIT 1
        """,
        (name,),
    )

    # Major history — rolling 52-week window
    history = query(
        f"""
        SELECT tournament_name, tournament_date, "placing", player_count,
               normalized_placement, wins, losses, deck_name, tier, tournament_id
        FROM {MARTS}.MAJOR_PLAYER_HISTORY
        WHERE lower(player_name) = lower(?)
          AND tournament_date >= current_date - INTERVAL '52 weeks'
        ORDER BY tournament_date DESC
        """,
        (name,),
    )

    if not history:
        return jsonify({"error": "player not found"}), 404

    # Archetype breakdown — rolling 52-week window
    archetypes = query(
        f"""
        SELECT deck_name,
               max(deck_sprite)                                                  AS deck_sprite,
               count(*)                                                          AS times_played,
               round(avg("placing"), 1)                                            AS avg_placement,
               round(sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3)  AS win_rate,
               min("placing")                                                      AS best_placing,
               min(tournament_date)                                              AS first_played,
               max(tournament_date)                                              AS last_played
        FROM {MARTS}.MAJOR_PLAYER_HISTORY
        WHERE lower(player_name) = lower(?)
          AND deck_name IS NOT NULL
          AND tournament_date >= current_date - INTERVAL '52 weeks'
        GROUP BY deck_name
        ORDER BY times_played DESC
        """,
        (name,),
    )

    return jsonify({
        "ranking": ranking[0] if ranking else None,
        "history":   history,
        "archetypes": archetypes,
    })


@app.get("/api/online/leaderboard")
def online_leaderboard():
    def _fetch():
        return query(
        f"""
        WITH top_decks AS (
            SELECT player_username, deck_name, deck_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY player_username
                       ORDER BY times_played DESC
                   ) AS rn
            FROM {MARTS}.PLAYER_ARCHETYPE_HISTORY
        )
        SELECT pe.player_username, pe.player_name, pe.country,
               pe.tournaments_entered, pe.total_wins, pe.total_losses,
               round(pe.win_rate * 100, 1)        AS win_rate_pct,
               pe.best_placement, pe.top8_finishes, pe.top_cut_count,
               round(pe.top_cut_rate * 100, 1)    AS top_cut_pct,
               round(pe.avg_placement, 1)         AS avg_placement,
               round(pe.glicko_rating, 0)         AS glicko_rating,
               round(pe.glicko_rd, 0)             AS glicko_rd,
               round(pe.glicko_rating_low, 0)     AS glicko_low,
               round(pe.glicko_rating_high, 0)    AS glicko_high,
               pe.last_played::date::varchar      AS last_played,
               td.deck_name                       AS top_deck_name,
               td.deck_id                         AS top_deck_id
        FROM {MARTS}.PLAYERS_ENRICHED pe
        LEFT JOIN top_decks td
          ON pe.player_username = td.player_username AND td.rn = 1
        WHERE pe.glicko_rating IS NOT NULL
          AND pe.tournaments_entered >= 3
        ORDER BY pe.glicko_rating_low DESC
        LIMIT 100
        """
        )
    return jsonify(cached("online_leaderboard", _fetch))


@app.get("/api/online/archetype-aces")
def online_archetype_aces():
    """Top online player per archetype, ranked by win rate (min 3 events with that deck)."""
    def _fetch():
        return query(
        f"""
        WITH ranked AS (
            SELECT
                pah.deck_id,
                pah.deck_name,
                pah.player_username,
                COALESCE(pe.player_name, pah.player_username) AS player_name,
                pe.country,
                pah.times_played,
                ROUND(pah.win_rate * 100, 1)    AS win_rate_pct,
                ROUND(pah.avg_placement, 1)      AS avg_placement,
                ROUND(pe.glicko_rating, 0)       AS glicko_rating,
                ROW_NUMBER() OVER (
                    PARTITION BY pah.deck_id
                    ORDER BY pah.win_rate DESC NULLS LAST, pah.times_played DESC
                ) AS rn
            FROM {MARTS}.PLAYER_ARCHETYPE_HISTORY pah
            LEFT JOIN {MARTS}.PLAYERS_ENRICHED pe
              ON pah.player_username = pe.player_username
            WHERE pah.times_played >= 3
              AND pah.win_rate > 0.55
        )
        SELECT deck_id, deck_name, player_username, player_name, country,
               times_played, win_rate_pct, avg_placement, glicko_rating
        FROM ranked
        WHERE rn = 1
        ORDER BY times_played DESC
        LIMIT 8
        """
        )
    return jsonify(cached("online_aces", _fetch))


@app.get("/api/tech/decks")
def tech_decks():
    source = request.args.get("source", "online")  # online | majors | both

    def _fetch():
        if source == "majors":
            # Decks that have major-tournament decklist data
            return query(
                f"""
                SELECT mca.deck_id,
                       MAX(ms.deck_name) AS deck_name,
                       MAX(mca.total_lists) AS total_lists
                FROM {MARTS}.MAJOR_CARD_ARCHETYPE_STATS mca
                LEFT JOIN raw.major_standings ms ON mca.deck_id = ms.deck_id
                GROUP BY mca.deck_id
                HAVING MAX(mca.total_lists) >= 3
                ORDER BY MAX(mca.total_lists) DESC
                LIMIT 60
                """
            )
        elif source == "both":
            return query(
                f"""
                SELECT deck_id, deck_name, total_lists FROM (
                    SELECT cas.deck_id,
                           MAX(pah.deck_name) AS deck_name,
                           MAX(cas.total_lists) AS total_lists
                    FROM {MARTS}.CARD_ARCHETYPE_STATS cas
                    LEFT JOIN {MARTS}.PLAYER_ARCHETYPE_HISTORY pah ON cas.deck_id = pah.deck_id
                    GROUP BY cas.deck_id HAVING MAX(cas.total_lists) >= 3
                    UNION
                    SELECT mca.deck_id,
                           MAX(ms.deck_name) AS deck_name,
                           MAX(mca.total_lists) AS total_lists
                    FROM {MARTS}.MAJOR_CARD_ARCHETYPE_STATS mca
                    LEFT JOIN raw.major_standings ms ON mca.deck_id = ms.deck_id
                    GROUP BY mca.deck_id HAVING MAX(mca.total_lists) >= 3
                ) combined
                ORDER BY total_lists DESC LIMIT 60
                """
            )
        else:  # online (default)
            return query(
                f"""
                SELECT cas.deck_id,
                       MAX(pah.deck_name) AS deck_name,
                       MAX(cas.total_lists) AS total_lists
                FROM {MARTS}.CARD_ARCHETYPE_STATS cas
                LEFT JOIN {MARTS}.PLAYER_ARCHETYPE_HISTORY pah ON cas.deck_id = pah.deck_id
                GROUP BY cas.deck_id
                HAVING MAX(cas.total_lists) >= 3
                ORDER BY MAX(cas.total_lists) DESC
                LIMIT 60
                """
            )

    return jsonify(cached(f"tech_decks:{source}", _fetch))


@app.get("/api/tech/<path:deck_id>")
def tech_cards(deck_id):
    source = request.args.get("source", "online")

    def _build_query(stat_table):
        return (
            f"""
            SELECT card_category,
                   card_name,
                   card_set,
                   lists_with_card,
                   total_lists,
                   round(inclusion_rate * 100, 1)   AS inclusion_pct,
                   round(avg_count_when_included, 2) AS avg_copies,
                   is_tech_card
            FROM {stat_table}
            WHERE deck_id = ?
            ORDER BY card_category, inclusion_rate DESC
            """,
            (deck_id,),
        )

    def _fetch():
        if source == "majors":
            sql, params = _build_query(f"{MARTS}.MAJOR_CARD_ARCHETYPE_STATS")
            return query(sql, params)
        elif source == "both":
            # Union both tables, keep highest inclusion_rate per card
            return query(
                f"""
                SELECT card_category, card_name, card_set,
                       SUM(lists_with_card) AS lists_with_card,
                       SUM(total_lists)     AS total_lists,
                       ROUND(SUM(lists_with_card)::float / SUM(total_lists) * 100, 1) AS inclusion_pct,
                       ROUND(AVG(avg_count_when_included), 2) AS avg_copies,
                       SUM(lists_with_card)::float / SUM(total_lists) < 0.30 AS is_tech_card
                FROM (
                    SELECT card_category, card_name, card_set,
                           lists_with_card, total_lists, avg_count_when_included
                    FROM {MARTS}.CARD_ARCHETYPE_STATS WHERE deck_id = ?
                    UNION ALL
                    SELECT card_category, card_name, card_set,
                           lists_with_card, total_lists, avg_count_when_included
                    FROM {MARTS}.MAJOR_CARD_ARCHETYPE_STATS WHERE deck_id = ?
                ) combined
                GROUP BY card_category, card_name, card_set
                ORDER BY card_category, inclusion_pct DESC
                """,
                (deck_id, deck_id),
            )
        else:
            sql, params = _build_query(f"{MARTS}.CARD_ARCHETYPE_STATS")
            return query(sql, params)

    return jsonify(cached(f"tech:{deck_id}:{source}", _fetch))


@app.get("/api/tech/<path:deck_id>/card-ev")
def tech_card_ev(deck_id):
    """Win-rate with vs without a specific card, broken down by opponent archetype."""
    card_name = request.args.get("card", "").strip()
    source    = request.args.get("source", "online")
    if not card_name:
        return jsonify({"error": "card param required"}), 400

    def _fetch():
        if source == "majors":
            rows = query(
                """
                WITH match_rows AS (
                    SELECT mm.labs_tournament_id AS tid, mm.player_id AS our,
                           mm.result,
                           -- Resolve opponent deck: prefer stored deck_id, fall back to name join
                           COALESCE(mm.opponent_deck_id, ms_opp.deck_id) AS opp_deck,
                           COALESCE(mm.opponent_deck_name,
                                    ms_opp.deck_name) AS opp_deck_name
                    FROM raw.major_matches mm
                    JOIN raw.major_standings ms_our
                      ON mm.labs_tournament_id = ms_our.labs_tournament_id
                     AND mm.player_id = ms_our.player_id
                     AND ms_our.deck_id = ?
                    LEFT JOIN raw.major_standings ms_opp
                      ON mm.labs_tournament_id = ms_opp.labs_tournament_id
                     AND mm.opponent_name = ms_opp.player_name
                    WHERE mm.result IN ('Win', 'Loss')
                      AND COALESCE(mm.opponent_deck_id, ms_opp.deck_id) IS NOT NULL
                      AND COALESCE(mm.opponent_deck_id, ms_opp.deck_id) != 'other'
                ),
                with_card AS (
                    SELECT mr.*,
                           COALESCE(md.card_count, 0) > 0 AS has_card
                    FROM match_rows mr
                    LEFT JOIN raw.major_decklists md
                      ON mr.tid = md.labs_tournament_id
                     AND mr.our = md.player_id
                     AND md.card_name = ?
                ),
                agg AS (
                    SELECT opp_deck, MAX(opp_deck_name) AS opp_deck_name, has_card,
                           COUNT(*) AS n,
                           ROUND(AVG(CASE WHEN result='Win' THEN 1.0 ELSE 0.0 END)*100,1) AS wr
                    FROM with_card GROUP BY opp_deck, has_card
                ),
                pivoted AS (
                    SELECT opp_deck, MAX(opp_deck_name) AS opp_deck_name,
                           MAX(CASE WHEN NOT has_card THEN n  END) AS n_without,
                           MAX(CASE WHEN NOT has_card THEN wr END) AS wr_without,
                           MAX(CASE WHEN has_card     THEN n  END) AS n_with,
                           MAX(CASE WHEN has_card     THEN wr END) AS wr_with,
                           SUM(n) AS total_n
                    FROM agg GROUP BY opp_deck HAVING SUM(n) >= 5
                )
                SELECT opp_deck, opp_deck_name, n_without, wr_without, n_with, wr_with,
                       ROUND(COALESCE(wr_with,wr_without) - COALESCE(wr_without,wr_with),1) AS delta
                FROM pivoted ORDER BY total_n DESC LIMIT 15
                """,
                (deck_id, card_name),
            )
        else:
            rows = query(
                """
                WITH match_rows AS (
                    SELECT m.tournament_id, m.player1 AS our, m.player2 AS opp,
                        CASE WHEN m.winner = m.player1 THEN true
                             WHEN m.winner = m.player2 THEN false
                             ELSE NULL END AS won
                    FROM raw.matches m
                    WHERE m.winner NOT IN ('0','-1')
                      AND m.player1 IS NOT NULL AND m.player2 IS NOT NULL
                    UNION ALL
                    SELECT m.tournament_id, m.player2, m.player1,
                        CASE WHEN m.winner = m.player2 THEN true
                             WHEN m.winner = m.player1 THEN false
                             ELSE NULL END
                    FROM raw.matches m
                    WHERE m.winner NOT IN ('0','-1')
                      AND m.player1 IS NOT NULL AND m.player2 IS NOT NULL
                ),
                with_decks AS (
                    SELECT mr.tournament_id, mr.our, mr.opp, mr.won,
                           s2.deck_id AS opp_deck, s2.deck_name AS opp_deck_name
                    FROM match_rows mr
                    JOIN raw.standings s1
                      ON mr.tournament_id = s1.tournament_id AND mr.our = s1.player_username
                     AND s1.deck_id = ?
                    JOIN raw.standings s2
                      ON mr.tournament_id = s2.tournament_id AND mr.opp = s2.player_username
                    WHERE mr.won IS NOT NULL AND s2.deck_id IS NOT NULL AND s2.deck_id != 'other'
                ),
                with_card AS (
                    SELECT wd.*, COALESCE(d.card_count, 0) > 0 AS has_card
                    FROM with_decks wd
                    LEFT JOIN raw.decklists d
                      ON wd.tournament_id = d.tournament_id
                     AND wd.our = d.player_username
                     AND d.card_name = ?
                ),
                agg AS (
                    SELECT opp_deck, MAX(opp_deck_name) AS opp_deck_name, has_card,
                           COUNT(*) AS n,
                           ROUND(AVG(CASE WHEN won THEN 1.0 ELSE 0.0 END)*100,1) AS wr
                    FROM with_card GROUP BY opp_deck, has_card
                ),
                pivoted AS (
                    SELECT opp_deck, MAX(opp_deck_name) AS opp_deck_name,
                           MAX(CASE WHEN NOT has_card THEN n  END) AS n_without,
                           MAX(CASE WHEN NOT has_card THEN wr END) AS wr_without,
                           MAX(CASE WHEN has_card     THEN n  END) AS n_with,
                           MAX(CASE WHEN has_card     THEN wr END) AS wr_with,
                           SUM(n) AS total_n
                    FROM agg GROUP BY opp_deck HAVING SUM(n) >= 10
                )
                SELECT opp_deck, opp_deck_name, n_without, wr_without, n_with, wr_with,
                       ROUND(COALESCE(wr_with,wr_without) - COALESCE(wr_without,wr_with),1) AS delta
                FROM pivoted ORDER BY total_n DESC LIMIT 15
                """,
                (deck_id, card_name),
            )
        return rows

    return jsonify(cached(f"card-ev:{deck_id}:{card_name}:{source}", _fetch))


@app.get("/api/player/<username>/vs/<opponent>")
def head_to_head(username, opponent):
    rows = query(
        f"""
        SELECT matches_played, wins, losses, ties, win_rate
        FROM {MARTS}.PLAYER_VS_PLAYER
        WHERE player_username = ? AND opponent_username = ?
        """,
        (username, opponent),
    )
    return jsonify(rows[0] if rows else {})


def _load_formats():
    fmts_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'formats.json')
    with open(fmts_path) as f:
        return sorted(json.load(f), key=lambda x: x['start'])


def _format_window(since: str):
    """Given a format start date string, return (format_name, since, until).
    until is the start of the *next* format so windows are strictly non-overlapping.
    For the current (latest) active format, until is None (no upper bound).
    """
    fmts  = _load_formats()
    today = date.today().isoformat()
    active = [f for f in fmts if f['start'] <= today]
    match  = next((f for f in reversed(active) if f['start'] <= since), None)
    fmt_name = match['name'] if match else since
    if match and match in active:
        idx   = active.index(match)
        until = active[idx + 1]['start'] if idx + 1 < len(active) else None
    else:
        until = None
    return fmt_name, since, until


def _current_format_cutoff():
    """Return (format_name, since, until) for the current Standard format.
    Falls back to the previous format if no majors have occurred yet in the
    newest set — so we never serve an empty meta page.
    Result is cached for CACHE_TTL seconds so the SELECT 1 probe doesn't
    fire on every /api/meta request.
    """
    def _resolve():
        fmts  = _load_formats()
        today = date.today().isoformat()
        active = [x for x in fmts if x['start'] <= today]
        if not active:
            return fmts[0]['name'], fmts[0]['start'], None
        current = active[-1]
        prev    = active[-2] if len(active) >= 2 else current
        rows = query(
            f"SELECT 1 FROM {MARTS}.MAJOR_PLAYER_HISTORY"
            f" WHERE tournament_date >= ? LIMIT 1",
            (current['start'],),
        )
        use   = current if rows else prev
        until = current['start'] if use is prev else None
        return use['name'], use['start'], until

    return cached("current_format_cutoff", _resolve)


@app.get("/api/formats")
def formats_list():
    """All known formats, sorted oldest-first, for the meta format selector."""
    fmts  = _load_formats()
    today = date.today().isoformat()
    # Only return formats that have actually started
    return jsonify([f for f in fmts if f['start'] <= today])


@app.get("/api/meta")
def meta_stats():
    since_override = request.args.get("since")
    if since_override:
        fmt_name, cutoff, until = _format_window(since_override)
    else:
        fmt_name, cutoff, until = _current_format_cutoff()

    cache_key = f"meta:{cutoff}"
    until_clause = "AND tournament_date < ?" if until else ""
    params = (cutoff, until) if until else (cutoff,)

    def _fetch():
        rows = query(
            f"""
            SELECT deck_name,
                   max(deck_sprite)                                                as deck_sprite,
                   count(*)                                                        as appearances,
                   round(avg("placing"), 1)                                          as avg_placement,
                   sum(case when "placing" <= 8 then 1 else 0 end)                   as top8s,
                   round(
                       sum(case when "placing" <= 8 then 1 else 0 end)::float
                       / nullif(count(*), 0), 3
                   )                                                               as top8_rate,
                   -- Day 2 proxy: top ~20 pct of field advances in a standard Swiss Regional.
                   -- player_count * 0.20, floored at 32, is a reasonable threshold.
                   round(
                       sum(case when "placing" <= greatest(32, round(player_count * 0.20))
                                then 1 else 0 end)::float
                       / nullif(count(*), 0), 3
                   )                                                               as day2_rate,
                   min("placing")                                                    as best_placing
            FROM {MARTS}.MAJOR_PLAYER_HISTORY
            WHERE deck_name is not null
              AND tournament_date >= ?
              {until_clause}
            GROUP BY deck_name
            ORDER BY appearances DESC
            LIMIT 30
            """,
            params,
        )
        return {"format": {"name": fmt_name, "since": cutoff}, "archetypes": rows}

    return jsonify(cached(cache_key, _fetch))


@app.get("/api/meta/top-finishes")
def meta_top_finishes():
    since_override = request.args.get("since")
    if since_override:
        _, cutoff, until = _format_window(since_override)
    else:
        _, cutoff, until = _current_format_cutoff()

    cache_key = f"meta_finishes:{cutoff}"
    until_clause = "AND tournament_date < ?" if until else ""
    params = (cutoff, until) if until else (cutoff,)

    def _fetch():
        rows = query(
            f"""
            SELECT deck_name, player_name, player_id, "placing",
                   tournament_name, tournament_date, tournament_id
            FROM {MARTS}.MAJOR_PLAYER_HISTORY
            WHERE deck_name IS NOT NULL
              AND "placing" <= 8
              AND tournament_date >= ?
              {until_clause}
            ORDER BY deck_name, "placing", tournament_date DESC
            LIMIT 500
            """,
            params,
        )
        result: dict = {}
        for r in rows:
            dk = r.get("DECK_NAME") or r.get("deck_name", "")
            if dk not in result:
                result[dk] = []
            result[dk].append({
                "player_name":     r.get("PLAYER_NAME")     or r.get("player_name", ""),
                "player_id":       r.get("PLAYER_ID")       or r.get("player_id"),
                "placing":         r.get("PLACING")         or r.get("placing"),
                "tournament_name": r.get("TOURNAMENT_NAME") or r.get("tournament_name", ""),
                "tournament_id":   r.get("TOURNAMENT_ID")   or r.get("tournament_id", ""),
            })
        return result

    return jsonify(cached(cache_key, _fetch))


@app.get("/api/leaderboard")
def leaderboard_data():
    def _fetch():
        return query(
            f"""
            SELECT sr.rank, sr.player_name, sr.player_id, sr.country,
                   sr.majors_counted, sr.win_rate_pct, sr.best_placing,
                   sr.top8s, sr.top16s, sr.worlds_top8s, sr.ic_top8s, sr.regional_top8s,
                   sr.atp_score, sr.avg_placement_pct,
                   sr.last_played, sr.title,
                   sr.worlds_score, sr.ic_score, sr.regional_score,
                   sr.top_deck_name, sr.top_deck_sprite,
                   mgl.archetype AS gym_leader_of
            FROM {MARTS}.SEASONAL_RANKINGS sr
            LEFT JOIN {MARTS}.MAJOR_GYM_LEADERS mgl
              ON lower(sr.player_name) = lower(mgl.player_name)
              AND mgl.is_gym_leader = TRUE
            ORDER BY sr.rank
            LIMIT 100
            """
        )
    return jsonify(cached("leaderboard", _fetch))


@app.get("/api/player-card/<name>")
def player_card(name):
    rows = query(
        f"""
        SELECT sr.rank, sr.player_name, sr.player_id, sr.country, sr.majors_counted, sr.win_rate_pct,
               sr.best_placing, sr.top8s, sr.atp_score, sr.title,
               sr.top_deck_name, sr.top_deck_sprite,
               sr.worlds_top8s, sr.ic_top8s, sr.regional_top8s,
               mgl.archetype AS gym_leader_of
        FROM {MARTS}.SEASONAL_RANKINGS sr
        LEFT JOIN {MARTS}.MAJOR_GYM_LEADERS mgl
          ON lower(sr.player_name) = lower(mgl.player_name)
          AND mgl.is_gym_leader = TRUE
        WHERE lower(sr.player_name) = lower(?)
        LIMIT 1
        """,
        (name,),
    )
    if not rows:
        return jsonify({"error": "not found"}), 404
    return jsonify(rows[0])


@app.get("/api/gym-leaders")
def gym_leaders_data():
    def _fetch():
        return query(
            f"""
            SELECT archetype, deck_sprite, player_name, player_id,
                   tournament_count, win_rate_pct, gym_leader_score, last_played
            FROM {MARTS}.MAJOR_GYM_LEADERS
            WHERE is_gym_leader = TRUE
            ORDER BY gym_leader_score DESC
            LIMIT 30
            """
        )
    return jsonify(cached("gym_leaders", _fetch))


# ---------------------------------------------------------------------------
# OG image  (/og-image.png)
# ---------------------------------------------------------------------------
import io
from PIL import Image as PilImage, ImageDraw, ImageFont

_OG_CACHE: dict = {}          # { 'img': bytes, 'ts': float }
_OG_TTL   = 300               # regenerate every 5 min

_STATIC = os.path.join(os.path.dirname(__file__), "static")

def _load_font(name: str, size: int):
    path = os.path.join(_STATIC, name)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _hex(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _build_og_image() -> bytes:
    W, H = 1200, 630
    BG      = _hex("07051a")
    SURFACE = _hex("0e0c26")
    BORDER  = _hex("2b2760")
    VIOLET  = _hex("a07cf8")
    ELECTRIC= _hex("ffd600")
    MUTED   = _hex("7b72b0")
    GREEN   = _hex("57f287")
    WHITE   = _hex("ede9ff")
    GOLD    = _hex("ffab00")

    img  = PilImage.new("RGBA", (W, H), BG + (255,))
    draw = ImageDraw.Draw(img)

    # Subtle dot grid
    for gx in range(0, W, 28):
        for gy in range(0, H, 28):
            draw.ellipse([gx-1, gy-1, gx+1, gy+1],
                         fill=VIOLET + (28,))

    # Small quarter-circle glow, top-left corner only
    for r in range(110, 0, -4):
        alpha = max(0, int(12 * (1 - r / 110)))
        draw.ellipse([-r, -r, r, r], fill=VIOLET + (alpha,))

    f_word  = _load_font("nunito_900.ttf", 88)   # big two-line wordmark
    f_large = _load_font("nunito_900.ttf", 48)
    f_med   = _load_font("nunito_800.ttf", 32)
    f_small = _load_font("nunito_800.ttf", 22)
    f_tiny  = _load_font("nunito_800.ttf", 18)

    # ── Left column: stacked two-line wordmark ────────────────────────────
    PAD   = 60
    # Card lives at CARD_X=730; left column center = (PAD + 730) / 2 ≈ 395
    LC    = (PAD + 730) // 2   # horizontal centre of left column

    # Measure so we can centre both lines
    w_indigo  = draw.textlength("INDIGO",  font=f_word)
    w_circuit = draw.textlength("CIRCUIT", font=f_word)

    # Vertically centre the block (wordmark + tagline) in the canvas
    block_h = 88 + 10 + 88 + 28 + 26   # line1 + gap + line2 + gap + tagline
    y_start = (H - block_h) // 2

    draw.text((LC - w_indigo  // 2, y_start),        "INDIGO",  font=f_word, fill=VIOLET   + (255,))
    draw.text((LC - w_circuit // 2, y_start + 98),   "CIRCUIT", font=f_word, fill=ELECTRIC + (255,))

    tagline = "welcome to the circuit"
    tw = draw.textlength(tagline, font=f_small)
    draw.text((LC - tw // 2, y_start + 98 + 96), tagline, font=f_small, fill=MUTED + (200,))

    # ── Right column: Champion card ───────────────────────────────────────
    CARD_X, CARD_Y = 730, 55
    CARD_W, CARD_H = 445, 520
    RADIUS = 18

    # Card shadow
    for i in range(10, 0, -1):
        a = int(50 * (i / 10))
        draw.rounded_rectangle(
            [CARD_X + 6, CARD_Y + 6, CARD_X + CARD_W + 6, CARD_Y + CARD_H + 6],
            radius=RADIUS, fill=(0, 0, 0, a)
        )

    # Card background
    draw.rounded_rectangle(
        [CARD_X, CARD_Y, CARD_X + CARD_W, CARD_Y + CARD_H],
        radius=RADIUS, fill=SURFACE + (255,),
        outline=GOLD + (120,), width=2
    )

    # Gold top accent bar
    draw.rounded_rectangle(
        [CARD_X, CARD_Y, CARD_X + CARD_W, CARD_Y + 5],
        radius=RADIUS, fill=GOLD + (200,)
    )

    # Fetch champion data
    champ = None
    try:
        rows = query(
            f"""
            SELECT sr.rank, sr.player_name, sr.country, sr.atp_score,
                   sr.win_rate_pct, sr.top8s, sr.best_placing,
                   sr.top_deck_name, sr.top_deck_sprite, sr.majors_counted
            FROM {MARTS}.SEASONAL_RANKINGS sr
            WHERE sr.rank = 1
            LIMIT 1
            """
        )
        champ = rows[0] if rows else None
    except Exception:
        pass

    cx = CARD_X + CARD_W // 2
    # Sprite sits right below the badge — tighter gap
    sprite_y = CARD_Y + 62

    # Deck sprite
    if champ:
        sprite_url = champ.get("TOP_DECK_SPRITE") or champ.get("top_deck_sprite")
        if not sprite_url:
            dn = (champ.get("TOP_DECK_NAME") or champ.get("top_deck_name") or "").lower()
            dn = re.sub(r"\b\w+'\s*s\s+", "", dn)
            dn = re.sub(r"\bmega\b|\bex\b|\bvstar\b|\bvmax\b|\bgx\b|\bv\b", "", dn)
            dn = re.sub(r"[\s-]+", "-", dn.strip()).strip("-")
            sprite_url = f"https://r2.limitlesstcg.net/pokemon/gen9/{dn.split('-')[0]}.png"
        try:
            resp = http.get(sprite_url, timeout=3)
            if resp.status_code == 200:
                spr = PilImage.open(io.BytesIO(resp.content)).convert("RGBA")
                spr = spr.resize((110, 110), PilImage.LANCZOS)
                img.paste(spr, (cx - 55, sprite_y), spr)
        except Exception:
            pass

    name  = (champ.get("PLAYER_NAME") or champ.get("player_name") or "—") if champ else "—"
    deck  = (champ.get("TOP_DECK_NAME") or champ.get("top_deck_name") or "") if champ else ""
    atp   = champ.get("ATP_SCORE") or champ.get("atp_score") if champ else None
    wr    = champ.get("WIN_RATE_PCT") or champ.get("win_rate_pct") if champ else None
    t8s   = champ.get("TOP8S") or champ.get("top8s") if champ else None
    majors= champ.get("MAJORS_COUNTED") or champ.get("majors_counted") if champ else None

    name_y = sprite_y + 120
    name_w = draw.textlength(name, font=f_large)
    draw.text((cx - name_w // 2, name_y), name, font=f_large, fill=WHITE + (255,))

    if deck:
        deck_w = draw.textlength(deck, font=f_small)
        draw.text((cx - deck_w // 2, name_y + 56), deck, font=f_small, fill=GOLD + (200,))

    # Divider
    div_y = name_y + 96
    draw.line([(CARD_X + 22, div_y), (CARD_X + CARD_W - 22, div_y)],
              fill=BORDER + (180,), width=1)

    # Stats grid — 3 cols
    stats = [
        (f"{atp:.1f}" if atp is not None else "—",  "ATP Score",  GOLD),
        (f"{wr}%"      if wr   is not None else "—", "Win Rate",   GREEN),
        (str(t8s)      if t8s  is not None else "—", "Top 8s",     VIOLET),
    ]
    col_w = CARD_W // 3
    for i, (val, label, color) in enumerate(stats):
        sx = CARD_X + col_w * i + col_w // 2
        sy = div_y + 22
        val_w = draw.textlength(val, font=f_med)
        draw.text((sx - val_w // 2, sy), val, font=f_med, fill=color + (255,))
        lbl_w = draw.textlength(label, font=f_tiny)
        draw.text((sx - lbl_w // 2, sy + 42), label, font=f_tiny, fill=MUTED + (200,))

    if majors:
        foot = f"{majors} major tournaments"
        fw = draw.textlength(foot, font=f_tiny)
        draw.text((cx - fw // 2, div_y + 110), foot, font=f_tiny, fill=MUTED + (160,))

    # CHAMPION badge — large, centered, bottom of card
    badge_h = 52
    badge_w = 220
    badge_y = CARD_Y + CARD_H - 26 - badge_h
    badge_x = cx - badge_w // 2
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=10, fill=_hex("2a1a00") + (230,),
        outline=GOLD + (180,), width=2
    )
    badge_label = "CHAMPION"
    bl_w = draw.textlength(badge_label, font=f_med)
    draw.text((cx - bl_w // 2, badge_y + (badge_h - 32) // 2), badge_label, font=f_med, fill=GOLD + (255,))

    draw.text((PAD, H - 36), "indigocircuit.app", font=f_tiny, fill=MUTED + (90,))

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


@app.get("/og-image.png")
def og_image():
    from flask import Response
    now = time.time()
    if "img" not in _OG_CACHE or now - _OG_CACHE["ts"] > _OG_TTL:
        _OG_CACHE["img"] = _build_og_image()
        _OG_CACHE["ts"]  = now
    return Response(_OG_CACHE["img"], mimetype="image/png",
                    headers={"Cache-Control": f"public, max-age={_OG_TTL}"})


# ---------------------------------------------------------------------------
# Startup pre-warm
# Populate the cache in the background so the first real visitor is fast.
# Runs once per worker process 3s after import.
# ---------------------------------------------------------------------------
def _prewarm():
    time.sleep(3)   # let gunicorn finish binding before we hit Snowflake
    tasks = [
        ("leaderboard",     lambda: app.test_client().get("/api/leaderboard")),
        ("gym_leaders",     lambda: app.test_client().get("/api/gym-leaders")),
        ("online_lb",       lambda: app.test_client().get("/api/online/leaderboard")),
        ("online_aces",     lambda: app.test_client().get("/api/online/archetype-aces")),
        ("meta",            lambda: app.test_client().get("/api/meta")),
        ("meta_finishes",   lambda: app.test_client().get("/api/meta/top-finishes")),
        ("tech_decks",      lambda: app.test_client().get("/api/tech/decks")),
    ]
    for name, fn in tasks:
        try:
            fn()
        except Exception:
            pass   # pre-warm is best-effort; real requests still work


_prewarm_thread = threading.Thread(target=_prewarm, daemon=True)
_prewarm_thread.start()


# ---------------------------------------------------------------------------
# Nightly pipeline scheduler (runs inside the dashboard process so it shares
# the /data volume without needing a separate Railway cron service)
# Fires at 06:00 UTC daily. Disable with DISABLE_SCHEDULER=1.
# ---------------------------------------------------------------------------
import fcntl
import subprocess

_PIPELINE_LOCK = str(Path(DUCKDB_PATH).parent / "pipeline.lock")


def _pid_has_duckdb_open(pid: int) -> bool:
    """Return True if any fd of pid points at the DuckDB file."""
    try:
        for fd in os.listdir(f'/proc/{pid}/fd'):
            try:
                if DUCKDB_PATH in os.readlink(f'/proc/{pid}/fd/{fd}'):
                    return True
            except OSError:
                pass
    except OSError:
        pass
    return False


def _close_own_duckdb_fds(logger) -> None:
    """Close any DuckDB file descriptors held by the current process so
    labs.py can acquire the write lock without inheriting them."""
    my_pid = os.getpid()
    try:
        for fd_name in os.listdir(f'/proc/{my_pid}/fd'):
            try:
                if DUCKDB_PATH in os.readlink(f'/proc/{my_pid}/fd/{fd_name}'):
                    try:
                        os.close(int(fd_name))
                        logger.info(f"[pipeline] Closed own DuckDB fd {fd_name}")
                    except OSError:
                        pass
            except OSError:
                pass
    except Exception as exc:
        logger.warning(f"[pipeline] _close_own_duckdb_fds error: {exc}")


def _kill_db_holders(logger) -> None:
    """Kill anything blocking the DuckDB write lock:
    - SIGKILL orphaned ingest scripts (safe: we hold pipeline.lock)
    - SIGTERM the sibling gunicorn worker if it has DuckDB open
      (gunicorn restarts it immediately, fresh with no DB fd)
    """
    import signal
    _INGEST_SCRIPTS = ("ingest/labs.py", "ingest/load.py", "ingest/glicko.py")
    my_pid = os.getpid()
    killed = False
    try:
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == my_pid:
                continue
            try:
                cmdline = open(f'/proc/{pid}/cmdline').read().replace('\x00', ' ')
                if any(s in cmdline for s in _INGEST_SCRIPTS):
                    logger.info(f"[pipeline] Killing orphaned ingest PID {pid}: {cmdline.strip()[:80]}")
                    os.kill(pid, signal.SIGKILL)
                    killed = True
                elif 'gunicorn' in cmdline and _pid_has_duckdb_open(pid):
                    logger.info(f"[pipeline] Recycling gunicorn worker PID {pid} to release DuckDB lock")
                    os.kill(pid, signal.SIGTERM)
                    killed = True
            except OSError:
                pass
    except Exception as exc:
        logger.warning(f"[pipeline] _kill_db_holders error: {exc}")
    if killed:
        time.sleep(5)  # allow gunicorn to restart recycled worker and OS to release fds


def _run_pipeline():
    global _pipeline_running
    log = app.logger

    # File-based mutex: only one gunicorn worker runs the pipeline at a time.
    # (Each worker spawns its own scheduler thread; without this they'd race.)
    try:
        lf = open(_PIPELINE_LOCK, "w")
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log.info("[pipeline] Another worker holds the lock — skipping this run")
        return

    log.info("[pipeline] Starting nightly run")

    # Shadow-copy the live DB so pipeline writes go to a separate file.
    # Readers keep hitting the live file uninterrupted; the signal file
    # is only held for <1 second during the final atomic rename.
    shadow = DUCKDB_PATH + ".shadow"
    using_shadow = False
    if os.path.exists(DUCKDB_PATH):
        try:
            shutil.copy2(DUCKDB_PATH, shadow)
            wal = DUCKDB_PATH + ".wal"
            if os.path.exists(wal):
                shutil.copy2(wal, shadow + ".wal")
            using_shadow = True
            log.info("[pipeline] Shadow DB ready — site stays live during pipeline")
        except Exception as e:
            log.error(f"[pipeline] Shadow copy failed ({e}) — falling back to live write")

    if not using_shadow:
        # First boot or copy failed: write directly to live path (old behaviour).
        # Signal file needed for the full duration in this case.
        Path(_PIPELINE_SIGNAL).touch()
        _kill_db_holders(log)
        _close_own_duckdb_fds(log)
        time.sleep(2)

    db_target = shadow if using_shadow else DUCKDB_PATH
    env = {**os.environ, "DUCKDB_PATH": db_target}

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    steps = [
        ["python3", "ingest/labs.py"],
        ["python3", "ingest/load.py"],
        ["python3", "ingest/glicko.py"],
        ["bash",    "ingest/pipeline.sh", "--dbt-only"],
    ]

    success = True
    try:
        # Per-step timeouts: load.py can take 60+ min on first seed
        timeouts = {"ingest/load.py": 7200, "ingest/labs.py": 1800,
                    "ingest/glicko.py": 600, "ingest/pipeline.sh": 600}
        for cmd in steps:
            step_timeout = timeouts.get(cmd[1], 1800)
            for attempt in range(40):  # retry up to 39× if DuckDB locked
                try:
                    result = subprocess.run(
                        cmd, cwd=base, env=env,
                        capture_output=True, text=True, timeout=step_timeout,
                    )
                    if result.returncode != 0:
                        if "Could not set lock" in result.stderr and attempt < 39:
                            log.warning(f"[pipeline] {cmd[1]} lock conflict, retrying in 15s (attempt {attempt+1})")
                            time.sleep(15)
                            continue
                        out = (result.stdout + result.stderr)[-3000:]
                        log.error(f"[pipeline] {cmd[1]} failed:\n{out}")
                        success = False
                    else:
                        log.info(f"[pipeline] {cmd[1]} done")
                    break
                except Exception as e:
                    log.error(f"[pipeline] {cmd[1]} error: {e}")
                    success = False
                    break
            if not success:
                break
    finally:
        if using_shadow:
            if success and os.path.exists(shadow):
                # Atomic swap: brief signal file window (<1 second)
                Path(_PIPELINE_SIGNAL).touch()
                time.sleep(0.5)  # let any in-flight reads close their connections
                try:
                    os.replace(shadow, DUCKDB_PATH)
                    log.info("[pipeline] Shadow DB swapped to live")
                except Exception as e:
                    log.error(f"[pipeline] Shadow swap failed: {e}")
                finally:
                    Path(_PIPELINE_SIGNAL).unlink(missing_ok=True)
            else:
                # Pipeline failed — discard shadow, live DB untouched
                log.info("[pipeline] Pipeline failed — shadow discarded, live DB unchanged")
                Path(shadow).unlink(missing_ok=True)
                try:
                    Path(shadow + ".wal").unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            Path(_PIPELINE_SIGNAL).unlink(missing_ok=True)

        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
        except Exception:
            pass

    bust_cache()
    log.info("[pipeline] Complete — cache cleared")


def _start_scheduler():
    from datetime import datetime, timezone
    import time as _time

    def _marts_ready():
        """Return True only if the DB file exists AND dbt marts have been built."""
        if not os.path.exists(DUCKDB_PATH):
            return False
        if os.path.exists(_PIPELINE_SIGNAL):
            return True  # pipeline is running, assume marts exist
        try:
            conn = duckdb.connect(DUCKDB_PATH, read_only=True)
            schemas = [r[0] for r in conn.execute("SELECT schema_name FROM information_schema.schemata").fetchall()]
            conn.close()
            return "dbt_dev_marts" in schemas
        except Exception:
            return False

    def _loop():
        # Bootstrap: run pipeline if DB is missing OR marts haven't been built yet
        if not _marts_ready():
            app.logger.info("[pipeline] Marts not ready — running initial seed now")
            _run_pipeline()

        while True:
            now = datetime.now(timezone.utc)
            # Fire at 06:00 UTC
            if now.hour == 6 and now.minute == 0:
                _run_pipeline()
                _time.sleep(61)   # skip remainder of this minute
            else:
                _time.sleep(30)

    t = threading.Thread(target=_loop, daemon=True, name="pipeline-scheduler")
    t.start()


# Clean up any stale signal file left by a previously interrupted pipeline.
try:
    Path(_PIPELINE_SIGNAL).unlink(missing_ok=True)
except Exception:
    pass

if not os.environ.get("DISABLE_SCHEDULER"):
    _start_scheduler()


@app.route("/admin/db-holders")
def admin_db_holders():
    secret = os.environ.get("ADMIN_SECRET", "")
    if not secret or request.args.get("secret") != secret:
        return jsonify({"error": "unauthorized"}), 403
    holders = []
    my_pid = os.getpid()
    try:
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                cmdline = open(f'/proc/{pid}/cmdline').read().replace('\x00', ' ').strip()
                for fd in os.listdir(f'/proc/{pid}/fd'):
                    try:
                        link = os.readlink(f'/proc/{pid}/fd/{fd}')
                        if DUCKDB_PATH in link:
                            holders.append({"pid": pid, "cmdline": cmdline[:120], "is_me": pid == my_pid})
                            break
                    except OSError:
                        pass
            except OSError:
                pass
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "duckdb_path": DUCKDB_PATH,
        "signal_file_exists": os.path.exists(_PIPELINE_SIGNAL),
        "holders": holders
    })


@app.route("/admin/debug-player")
def admin_debug_player():
    secret = os.environ.get("ADMIN_SECRET", "")
    if not secret or request.args.get("secret") != secret:
        return jsonify({"error": "unauthorized"}), 403
    username = request.args.get("username", "")
    if not username:
        return jsonify({"error": "username required"}), 400
    results = {}
    results["players_enriched"] = query(
        f"SELECT * FROM {MARTS}.PLAYERS_ENRICHED WHERE lower(player_username) = lower(?)", [username]
    )
    results["glicko_raw"] = query(
        "SELECT * FROM raw.glicko_ratings WHERE lower(player_username) = lower(?)", [username]
    )
    results["match_count"] = query(
        "SELECT COUNT(*) AS cnt FROM raw.matches WHERE lower(player1) = lower(?) OR lower(player2) = lower(?)", [username, username]
    )
    results["tournaments"] = query(
        "SELECT DISTINCT tournament_id FROM raw.matches WHERE lower(player1) = lower(?) OR lower(player2) = lower(?) ORDER BY tournament_id DESC LIMIT 20", [username, username]
    )
    results["online_rank"] = query(
        f"""SELECT rank() OVER (ORDER BY glicko_rating_low DESC) AS rnk, player_username, glicko_rating, glicko_rd, glicko_rating_low
            FROM {MARTS}.PLAYERS_ENRICHED WHERE glicko_rating IS NOT NULL AND tournaments_entered >= 3
            QUALIFY lower(player_username) = lower(?)""", [username]
    )
    return jsonify(results)


@app.route("/admin/run-pipeline", methods=["POST"])
def admin_run_pipeline():
    """Manually trigger the ingest pipeline (protected by ADMIN_SECRET env var)."""
    secret = os.environ.get("ADMIN_SECRET", "")
    if not secret or request.headers.get("X-Admin-Secret") != secret:
        return jsonify({"error": "unauthorized"}), 403
    t = threading.Thread(target=_run_pipeline, daemon=True, name="pipeline-manual")
    t.start()
    return jsonify({"status": "started"}), 202


def _run_majors_scrape():
    """
    Scrape full-field decklists + matches for all majors, then rebuild dbt marts.
    Incremental: already-cached players are skipped automatically.
    Expected runtime: 4-8 hours for 65 tournaments × full field.
    """
    log = app.logger
    try:
        lf = open(_PIPELINE_LOCK, "w")
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log.info("[majors-scrape] Pipeline lock held — skipping")
        return

    log.info("[majors-scrape] Starting full-field decklist scrape")
    # No signal file — labs.py now opens a fresh connection per tournament so
    # the DB is only locked during brief write bursts, not the full 8h run.
    # Dashboard reads proceed normally between bursts.

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    steps = [
        (["python3", "ingest/labs.py", "--with-decklists", "--all-players"], 28800),  # 8h
        (["bash", "ingest/pipeline.sh", "--dbt-only"], 600),
    ]

    try:
        for cmd, step_timeout in steps:
            for attempt in range(40):
                try:
                    result = subprocess.run(cmd, cwd=base, capture_output=True, text=True, timeout=step_timeout)
                    if result.returncode != 0:
                        if "Could not set lock" in result.stderr and attempt < 39:
                            log.warning(f"[majors-scrape] {cmd[1]} lock conflict, retrying in 15s")
                            time.sleep(15)
                            continue
                        out = (result.stdout + result.stderr)[-3000:]
                        log.error(f"[majors-scrape] {cmd[1]} failed:\n{out}")
                    else:
                        log.info(f"[majors-scrape] {cmd[1]} done")
                    break
                except subprocess.TimeoutExpired:
                    log.error(f"[majors-scrape] {cmd[1]} timed out after {step_timeout}s")
                    break
                except Exception as e:
                    log.error(f"[majors-scrape] {cmd[1]} error: {e}")
                    break
    finally:
        try:
            Path(_PIPELINE_SIGNAL).unlink(missing_ok=True)
        except Exception:
            pass
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
        except Exception:
            pass

    bust_cache()
    log.info("[majors-scrape] Complete — cache cleared")


@app.route("/admin/run-majors-scrape", methods=["POST"])
def admin_run_majors_scrape():
    """
    Kick off full-field majors decklist scrape in the background.
    Incremental: already-scraped players are skipped.
    Protected by X-Admin-Secret header.
    """
    secret = os.environ.get("ADMIN_SECRET", "")
    if not secret or request.headers.get("X-Admin-Secret") != secret:
        return jsonify({"error": "unauthorized"}), 403
    t = threading.Thread(target=_run_majors_scrape, daemon=True, name="majors-scrape")
    t.start()
    return jsonify({"status": "started", "note": "full-field scrape running; check Railway logs"}), 202


if __name__ == "__main__":
    app.run(debug=True, port=5001)
