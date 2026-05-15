"""
Indigo Circuit — Flask dashboard.
Reads from Snowflake PTCG_SCOUTING via environment variables.
"""
import os

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
            min(placing)                    AS best_placement
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE lower(player_name) LIKE lower(%s)
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


@app.get("/api/meta")
def meta_stats():
    # Majors only, rolling 52-week window — same window as seasonal_rankings
    rows = query(
        f"""
        SELECT deck_name,
               count(*)                                                        as appearances,
               round(avg(placing), 1)                                          as avg_placement,
               round(sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3) as win_rate,
               sum(case when placing <= 8 then 1 else 0 end)                   as top8s
        FROM PTCG_SCOUTING.{MARTS}.MAJOR_PLAYER_HISTORY
        WHERE deck_name is not null
          AND tournament_date >= dateadd('week', -52, current_date())
        GROUP BY deck_name
        ORDER BY appearances DESC
        LIMIT 30
        """
    )
    return jsonify(rows)


@app.get("/api/leaderboard")
def leaderboard_data():
    # Rankings from majors only — ATP-style seasonal points
    rows = query(
        f"""
        SELECT rank, player_name, country,
               majors_counted, win_rate_pct, best_placing,
               top8s, top16s, worlds_top8s, ic_top8s, regional_top8s,
               atp_score, avg_placement_pct,
               last_played, title,
               worlds_score, ic_score, regional_score,
               top_deck_name, top_deck_sprite
        FROM PTCG_SCOUTING.{MARTS}.SEASONAL_RANKINGS
        ORDER BY rank
        LIMIT 100
        """
    )
    return jsonify(rows)


@app.get("/api/player-card/<name>")
def player_card(name):
    rows = query(
        f"""
        SELECT rank, player_name, country, majors_counted, win_rate_pct,
               best_placing, top8s, atp_score, title,
               top_deck_name, top_deck_sprite,
               worlds_top8s, ic_top8s, regional_top8s
        FROM PTCG_SCOUTING.{MARTS}.SEASONAL_RANKINGS
        WHERE lower(player_name) = lower(%s)
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
        SELECT archetype, deck_sprite, player_name,
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
