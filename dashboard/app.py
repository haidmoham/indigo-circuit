"""
Indigo Circuit — Flask dashboard.
Reads from Snowflake PTCG_SCOUTING via environment variables.
"""
import json
import os
from datetime import date

import snowflake.connector
from flask import Flask, render_template, request, jsonify
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
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/player/<username>")
def player_report(username):
    return render_template("player.html", username=username)

@app.get("/meta")
def meta():
    return render_template("meta.html")

@app.get("/league")
def league():
    return render_template("league.html")

@app.get("/leaderboard")
def leaderboard():
    return render_template("leaderboard.html")


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
