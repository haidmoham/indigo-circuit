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

@app.get("/player/<username>")
def player_report(username):
    return render_template("player.html", username=username)

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

    # Try online PLAYERS mart first; fall back to major player history
    try:
        rows = query(
            f"""
            SELECT player_username, player_name, country,
                   tournaments_entered, best_placement
            FROM PTCG_SCOUTING.{MARTS}.PLAYERS
            WHERE lower(player_username) LIKE lower(%s)
               OR lower(player_name)     LIKE lower(%s)
            ORDER BY tournaments_entered DESC
            LIMIT 20
            """,
            (f"%{q}%", f"%{q}%"),
        )
        if rows:
            return jsonify(rows)
    except Exception:
        pass  # fall through to majors search

    # Fallback: search across major tournament players
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


@app.get("/api/player/<username>")
def player_stats(username):
    career = query(
        f"SELECT * FROM PTCG_SCOUTING.{MARTS}.PLAYERS_ENRICHED WHERE player_username = %s",
        (username,),
    )
    if not career:
        return jsonify({"error": "player not found"}), 404

    history = query(
        f"""
        SELECT tournament_name, tournament_date, placing, player_count,
               normalized_placement, wins, losses, ties, deck_name, format
        FROM PTCG_SCOUTING.{MARTS}.PLAYER_TOURNAMENT_HISTORY
        WHERE player_username = %s
        ORDER BY tournament_date DESC
        """,
        (username,),
    )

    archetypes = query(
        f"""
        SELECT deck_name, times_played, avg_placement, win_rate, archetype_loyalty,
               first_played, last_played
        FROM PTCG_SCOUTING.{MARTS}.PLAYER_ARCHETYPE_HISTORY
        WHERE player_username = %s
        ORDER BY times_played DESC
        """,
        (username,),
    )

    return jsonify({"career": career[0], "history": history, "archetypes": archetypes})


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


def _current_format_cutoff():
    """Return the start date of the current Standard format.
    Falls back to the previous format's start if no majors have occurred yet
    in the newest set — so we never serve an empty meta page.
    """
    fmts_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'formats.json')
    with open(fmts_path) as f:
        fmts = sorted(json.load(f), key=lambda x: x['start'])

    today = date.today().isoformat()
    active = [x for x in fmts if x['start'] <= today]
    if not active:
        return fmts[0]['start']

    current_start = active[-1]['start']
    prev_start    = active[-2]['start'] if len(active) >= 2 else current_start

    # Check whether any major has been played in the current format window
    rows = query(
        f"""
        SELECT 1 FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE tournament_date >= %s LIMIT 1
        """,
        (current_start,),
    )
    return current_start if rows else prev_start


@app.get("/api/meta")
def meta_stats():
    # Restrict to current Standard format only; fall back to prior format if
    # no majors have happened yet in the new set.
    cutoff = _current_format_cutoff()
    rows = query(
        f"""
        SELECT deck_name,
               count(*)                                                        as appearances,
               round(avg(placing), 1)                                          as avg_placement,
               round(sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3) as win_rate,
               sum(case when placing <= 8 then 1 else 0 end)                   as top8s
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE deck_name is not null
          AND tournament_date >= %s
        GROUP BY deck_name
        ORDER BY appearances DESC
        LIMIT 30
        """,
        (cutoff,),
    )
    return jsonify(rows)


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


if __name__ == "__main__":
    app.run(debug=True, port=5001)
