"""
Indigo Circuit — Flask dashboard.
Reads from Snowflake PTCG_SCOUTING via environment variables.
"""
import json
import os
import re
from datetime import date
from urllib.parse import quote as urlquote

import requests as http
import snowflake.connector
from flask import Flask, render_template, request, jsonify, redirect
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Schema where dbt marts live (override with env var in production)
MARTS = os.environ.get("DBT_MARTS_SCHEMA", "DBT_DEV_MARTS")


def get_conn():
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        database=os.environ.get("SNOWFLAKE_DATABASE", "PTCG_SCOUTING"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
    )


def query(sql, params=None):
    conn = get_conn()
    cur = conn.cursor(snowflake.connector.DictCursor)
    try:
        cur.execute(sql, params or ())
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


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
    global _lid_table_ready
    if _lid_table_ready:
        return
    try:
        query("""
            CREATE TABLE IF NOT EXISTS RAW.LIMITLESS_PLAYER_IDS (
                player_name_lower VARCHAR NOT NULL,
                limitless_id      VARCHAR,
                resolved_at       TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
            )
        """)
    except Exception:
        pass
    _lid_table_ready = True


def _resolve_limitless_id(name: str):
    """Return the limitlesstcg.com player ID for *name*, or None on failure."""
    _ensure_lid_table()
    key = name.lower().strip()

    if key in _lid_cache:
        return _lid_cache[key]

    # L2 — Snowflake cache
    try:
        rows = query(
            "SELECT limitless_id FROM RAW.LIMITLESS_PLAYER_IDS WHERE player_name_lower = %s",
            (key,),
        )
        if rows:
            lid = rows[0].get("LIMITLESS_ID")
            _lid_cache[key] = lid
            return lid
    except Exception:
        pass

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

    # Persist result (MERGE = upsert so re-runs overwrite stale entries)
    try:
        query(
            """
            MERGE INTO RAW.LIMITLESS_PLAYER_IDS t
            USING (SELECT %s AS k, %s AS v) s ON t.player_name_lower = s.k
            WHEN MATCHED THEN UPDATE SET limitless_id = s.v,
                                         resolved_at  = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (player_name_lower, limitless_id)
                                  VALUES (s.k, s.v)
            """,
            (key, lid),
        )
    except Exception:
        pass  # non-fatal; in-memory cache still works

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
            min(placing)                    AS best_placement,
            -- most recent player_id as canonical Limitless profile link
            max_by(player_id, tournament_date) AS player_id
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE lower(player_name) LIKE lower(%s)
          AND player_id IS NOT NULL
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
        FROM PTCG_SCOUTING.{MARTS}.SEASONAL_RANKINGS sr
        LEFT JOIN PTCG_SCOUTING.{MARTS}.MAJOR_GYM_LEADERS mgl
          ON lower(sr.player_name) = lower(mgl.player_name)
          AND mgl.is_gym_leader = TRUE
        WHERE lower(sr.player_name) = lower(%s)
        LIMIT 1
        """,
        (name,),
    )

    # Full major history (all tournaments, not just last 52 weeks)
    history = query(
        f"""
        SELECT tournament_name, tournament_date, placing, player_count,
               normalized_placement, wins, losses, deck_name, tier, tournament_id
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE lower(player_name) = lower(%s)
        ORDER BY tournament_date DESC
        """,
        (name,),
    )

    if not history:
        return jsonify({"error": "player not found"}), 404

    # Archetype breakdown across all history
    archetypes = query(
        f"""
        SELECT deck_name,
               max(deck_sprite)                                                  AS deck_sprite,
               count(*)                                                          AS times_played,
               round(avg(placing), 1)                                            AS avg_placement,
               round(sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3)  AS win_rate,
               min(placing)                                                      AS best_placing,
               min(tournament_date)                                              AS first_played,
               max(tournament_date)                                              AS last_played
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE lower(player_name) = lower(%s)
          AND deck_name IS NOT NULL
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
    rows = query(
        f"""
        WITH top_decks AS (
            SELECT player_username, deck_name, deck_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY player_username
                       ORDER BY times_played DESC
                   ) AS rn
            FROM PTCG_SCOUTING.{MARTS}.PLAYER_ARCHETYPE_HISTORY
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
        FROM PTCG_SCOUTING.{MARTS}.PLAYERS_ENRICHED pe
        LEFT JOIN top_decks td
          ON pe.player_username = td.player_username AND td.rn = 1
        WHERE pe.glicko_rating IS NOT NULL
          AND pe.tournaments_entered >= 3
        ORDER BY pe.glicko_rating_low DESC
        LIMIT 100
        """
    )
    return jsonify(rows)


@app.get("/api/online/archetype-aces")
def online_archetype_aces():
    """Top online player per archetype, ranked by win rate (min 3 events with that deck)."""
    rows = query(
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
            FROM PTCG_SCOUTING.{MARTS}.PLAYER_ARCHETYPE_HISTORY pah
            LEFT JOIN PTCG_SCOUTING.{MARTS}.PLAYERS_ENRICHED pe
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
    return jsonify(rows)


@app.get("/api/tech/decks")
def tech_decks():
    rows = query(
        f"""
        SELECT cas.deck_id,
               MAX(pah.deck_name) AS deck_name,
               MAX(cas.total_lists) AS total_lists
        FROM PTCG_SCOUTING.{MARTS}.CARD_ARCHETYPE_STATS cas
        LEFT JOIN PTCG_SCOUTING.{MARTS}.PLAYER_ARCHETYPE_HISTORY pah
          ON cas.deck_id = pah.deck_id
        GROUP BY cas.deck_id
        HAVING MAX(cas.total_lists) >= 3
        ORDER BY MAX(cas.total_lists) DESC
        LIMIT 60
        """
    )
    return jsonify(rows)


@app.get("/api/tech/<path:deck_id>")
def tech_cards(deck_id):
    rows = query(
        f"""
        SELECT card_category,
               card_name,
               card_set,
               lists_with_card,
               total_lists,
               round(inclusion_rate * 100, 1)       AS inclusion_pct,
               round(avg_count_when_included, 2)     AS avg_copies,
               is_tech_card
        FROM PTCG_SCOUTING.{MARTS}.CARD_ARCHETYPE_STATS
        WHERE deck_id = %s
        ORDER BY card_category,
                 inclusion_rate DESC
        """,
        (deck_id,),
    )
    return jsonify(rows)


@app.get("/api/player/<username>/vs/<opponent>")
def head_to_head(username, opponent):
    rows = query(
        f"""
        SELECT matches_played, wins, losses, ties, win_rate
        FROM PTCG_SCOUTING.{MARTS}.PLAYER_VS_PLAYER
        WHERE player_username = %s AND opponent_username = %s
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
    """
    fmts  = _load_formats()
    today = date.today().isoformat()
    active = [x for x in fmts if x['start'] <= today]
    if not active:
        return fmts[0]['name'], fmts[0]['start'], None

    current = active[-1]
    prev    = active[-2] if len(active) >= 2 else current

    rows = query(
        f"SELECT 1 FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY"
        f" WHERE tournament_date >= %s LIMIT 1",
        (current['start'],),
    )
    use = current if rows else prev
    # Current format has no upper bound; if we fell back, cap at current format's start
    until = current['start'] if use is prev else None
    return use['name'], use['start'], until


@app.get("/api/formats")
def formats_list():
    """All known formats, sorted oldest-first, for the meta format selector."""
    fmts  = _load_formats()
    today = date.today().isoformat()
    # Only return formats that have actually started
    return jsonify([f for f in fmts if f['start'] <= today])


@app.get("/api/meta")
def meta_stats():
    since_override = request.args.get("since")   # e.g. ?since=2026-04-25
    if since_override:
        fmt_name, cutoff, until = _format_window(since_override)
    else:
        fmt_name, cutoff, until = _current_format_cutoff()

    until_clause = "AND tournament_date < %s" if until else ""
    params = (cutoff, until) if until else (cutoff,)

    rows = query(
        f"""
        SELECT deck_name,
               max(deck_sprite)                                                as deck_sprite,
               count(*)                                                        as appearances,
               round(avg(placing), 1)                                          as avg_placement,
               sum(case when placing <= 8 then 1 else 0 end)                   as top8s,
               round(
                   sum(case when placing <= 8 then 1 else 0 end)::float
                   / nullif(count(*), 0), 3
               )                                                               as top8_rate,
               -- Day 2 proxy: top ~20 pct of field advances in a standard Swiss Regional.
               -- player_count * 0.20, floored at 32, is a reasonable threshold.
               round(
                   sum(case when placing <= greatest(32, round(player_count * 0.20))
                            then 1 else 0 end)::float
                   / nullif(count(*), 0), 3
               )                                                               as day2_rate,
               min(placing)                                                    as best_placing
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE deck_name is not null
          AND tournament_date >= %s
          {until_clause}
        GROUP BY deck_name
        ORDER BY appearances DESC
        LIMIT 30
        """,
        params,
    )
    return jsonify({"format": {"name": fmt_name, "since": cutoff}, "archetypes": rows})


@app.get("/api/meta/top-finishes")
def meta_top_finishes():
    since_override = request.args.get("since")
    if since_override:
        _, cutoff, until = _format_window(since_override)
    else:
        _, cutoff, until = _current_format_cutoff()

    until_clause = "AND tournament_date < %s" if until else ""
    params = (cutoff, until) if until else (cutoff,)

    rows = query(
        f"""
        SELECT deck_name, player_name, player_id, placing,
               tournament_name, tournament_date, tournament_id
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE deck_name IS NOT NULL
          AND placing <= 8
          AND tournament_date >= %s
          {until_clause}
        ORDER BY deck_name, placing, tournament_date DESC
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
    return jsonify(result)


@app.get("/api/leaderboard")
def leaderboard_data():
    # Rankings from majors only — ATP-style seasonal points with gym leader join
    rows = query(
        f"""
        SELECT sr.rank, sr.player_name, sr.player_id, sr.country,
               sr.majors_counted, sr.win_rate_pct, sr.best_placing,
               sr.top8s, sr.top16s, sr.worlds_top8s, sr.ic_top8s, sr.regional_top8s,
               sr.atp_score, sr.avg_placement_pct,
               sr.last_played, sr.title,
               sr.worlds_score, sr.ic_score, sr.regional_score,
               sr.top_deck_name, sr.top_deck_sprite,
               mgl.archetype AS gym_leader_of
        FROM PTCG_SCOUTING.{MARTS}.SEASONAL_RANKINGS sr
        LEFT JOIN PTCG_SCOUTING.{MARTS}.MAJOR_GYM_LEADERS mgl
          ON lower(sr.player_name) = lower(mgl.player_name)
          AND mgl.is_gym_leader = TRUE
        ORDER BY sr.rank
        LIMIT 100
        """
    )
    return jsonify(rows)


@app.get("/api/player-card/<name>")
def player_card(name):
    rows = query(
        f"""
        SELECT sr.rank, sr.player_name, sr.player_id, sr.country, sr.majors_counted, sr.win_rate_pct,
               sr.best_placing, sr.top8s, sr.atp_score, sr.title,
               sr.top_deck_name, sr.top_deck_sprite,
               sr.worlds_top8s, sr.ic_top8s, sr.regional_top8s,
               mgl.archetype AS gym_leader_of
        FROM PTCG_SCOUTING.{MARTS}.SEASONAL_RANKINGS sr
        LEFT JOIN PTCG_SCOUTING.{MARTS}.MAJOR_GYM_LEADERS mgl
          ON lower(sr.player_name) = lower(mgl.player_name)
          AND mgl.is_gym_leader = TRUE
        WHERE lower(sr.player_name) = lower(%s)
        LIMIT 1
        """,
        (name,),
    )
    if not rows:
        return jsonify({"error": "not found"}), 404
    return jsonify(rows[0])


@app.get("/api/gym-leaders")
def gym_leaders_data():
    # Gym Leaders from majors only
    rows = query(
        f"""
        SELECT archetype, deck_sprite, player_name, player_id,
               tournament_count, win_rate_pct, gym_leader_score, last_played
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_GYM_LEADERS
        WHERE is_gym_leader = TRUE
        ORDER BY gym_leader_score DESC
        LIMIT 30
        """
    )
    return jsonify(rows)


# ---------------------------------------------------------------------------
# OG image  (/og-image.png)
# ---------------------------------------------------------------------------
import io
import time
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

    # Subtle glow — bottom-left corner only, kept well away from text
    for r in range(200, 0, -4):
        alpha = max(0, int(10 * (1 - r / 200)))
        draw.ellipse([0-r, H-r, 0+r, H+r],
                     fill=VIOLET + (alpha,))

    f_huge  = _load_font("nunito_900.ttf", 72)
    f_large = _load_font("nunito_900.ttf", 48)
    f_med   = _load_font("nunito_800.ttf", 32)
    f_small = _load_font("nunito_800.ttf", 22)
    f_tiny  = _load_font("nunito_800.ttf", 18)

    # ── Left column: branding ─────────────────────────────────────────────
    PAD = 72
    y   = 120

    # "INDIGO" in violet, "CIRCUIT" in electric
    draw.text((PAD, y), "INDIGO ", font=f_huge, fill=VIOLET + (255,))
    w_indigo = draw.textlength("INDIGO ", font=f_huge)
    draw.text((PAD + w_indigo, y), "CIRCUIT", font=f_huge, fill=ELECTRIC + (255,))

    y += 86
    draw.text((PAD, y), "Welcome to the Circuit", font=f_med, fill=MUTED + (255,))

    y += 52
    draw.text((PAD, y), "Competitive Pokémon TCG Intelligence", font=f_small, fill=MUTED + (200,))

    # Thin separator line
    y += 48
    draw.line([(PAD, y), (530, y)], fill=BORDER + (200,), width=1)

    y += 24
    draw.text((PAD, y), "Rankings  ·  Meta  ·  Tech Scouting  ·  Glicko-2 Online", font=f_tiny, fill=MUTED + (160,))

    # ── Right column: Champion card ───────────────────────────────────────
    CARD_X, CARD_Y = 660, 90
    CARD_W, CARD_H = 465, 448
    RADIUS = 18

    # Card shadow
    shadow_offset = 8
    for i in range(12, 0, -1):
        alpha = int(60 * (i / 12))
        draw.rounded_rectangle(
            [CARD_X + shadow_offset, CARD_Y + shadow_offset,
             CARD_X + CARD_W + shadow_offset, CARD_Y + CARD_H + shadow_offset],
            radius=RADIUS, fill=(0, 0, 0, alpha)
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

    # Champion badge
    badge_x, badge_y = CARD_X + 22, CARD_Y + 26
    badge_w = 160
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + 30],
        radius=5, fill=_hex("2a1a00") + (220,),
        outline=GOLD + (160,), width=1
    )
    draw.text((badge_x + 8, badge_y + 6), "CHAMPION", font=f_tiny, fill=GOLD + (255,))

    # Fetch champion data
    champ = None
    try:
        rows = query(
            f"""
            SELECT sr.rank, sr.player_name, sr.country, sr.atp_score,
                   sr.win_rate_pct, sr.top8s, sr.best_placing,
                   sr.top_deck_name, sr.top_deck_sprite, sr.majors_counted
            FROM PTCG_SCOUTING.{MARTS}.SEASONAL_RANKINGS sr
            WHERE sr.rank = 1
            LIMIT 1
            """
        )
        champ = rows[0] if rows else None
    except Exception:
        pass

    cx = CARD_X + CARD_W // 2
    sprite_y = CARD_Y + 72

    # Deck sprite
    if champ:
        sprite_url = champ.get("TOP_DECK_SPRITE") or champ.get("top_deck_sprite")
        if not sprite_url:
            # build from deck name
            dn = (champ.get("TOP_DECK_NAME") or champ.get("top_deck_name") or "").lower()
            dn = re.sub(r"\b\w+'\s*s\s+", "", dn)
            dn = re.sub(r"\bmega\b|\bex\b|\bvstar\b|\bvmax\b|\bgx\b|\bv\b", "", dn)
            dn = re.sub(r"[\s-]+", "-", dn.strip()).strip("-")
            first = dn.split("-")[0]
            sprite_url = f"https://r2.limitlesstcg.net/pokemon/gen9/{first}.png"
        try:
            resp = http.get(sprite_url, timeout=3)
            if resp.status_code == 200:
                spr = PilImage.open(io.BytesIO(resp.content)).convert("RGBA")
                spr = spr.resize((96, 96), PilImage.LANCZOS)
                img.paste(spr, (cx - 48, sprite_y), spr)
        except Exception:
            pass

    name_y = sprite_y + 108
    name  = (champ.get("PLAYER_NAME") or champ.get("player_name") or "—") if champ else "—"
    deck  = (champ.get("TOP_DECK_NAME") or champ.get("top_deck_name") or "") if champ else ""
    atp   = champ.get("ATP_SCORE") or champ.get("atp_score") if champ else None
    wr    = champ.get("WIN_RATE_PCT") or champ.get("win_rate_pct") if champ else None
    t8s   = champ.get("TOP8S") or champ.get("top8s") if champ else None
    majors= champ.get("MAJORS_COUNTED") or champ.get("majors_counted") if champ else None

    # Name — center it
    name_w = draw.textlength(name, font=f_large)
    draw.text((cx - name_w // 2, name_y), name, font=f_large, fill=WHITE + (255,))

    if deck:
        deck_w = draw.textlength(deck, font=f_small)
        draw.text((cx - deck_w // 2, name_y + 58), deck,
                  font=f_small, fill=GOLD + (200,))

    # Divider
    div_y = name_y + 100
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
        sy = div_y + 20
        val_w = draw.textlength(val, font=f_med)
        draw.text((sx - val_w // 2, sy), val, font=f_med, fill=color + (255,))
        lbl_w = draw.textlength(label, font=f_tiny)
        draw.text((sx - lbl_w // 2, sy + 40), label, font=f_tiny, fill=MUTED + (200,))

    # Footer: majors count
    if majors:
        foot = f"{majors} major tournaments"
        fw = draw.textlength(foot, font=f_tiny)
        draw.text((cx - fw // 2, div_y + 108), foot, font=f_tiny, fill=MUTED + (160,))

    # Favicon bottom-left corner
    try:
        fav = PilImage.open(os.path.join(_STATIC, "favicon.png")).convert("RGBA")
        fav = fav.resize((40, 40), PilImage.NEAREST)
        img.paste(fav, (PAD, H - 64), fav)
    except Exception:
        pass

    draw.text((PAD + 52, H - 54), "indigocircuit", font=f_small, fill=VIOLET + (180,))

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


if __name__ == "__main__":
    app.run(debug=True, port=5001)
