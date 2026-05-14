"""
PTCG Scouting Report — Flask dashboard.
Reads from Snowflake PTCG_SCOUTING.MARTS via environment variables.
"""
import os
import json

import snowflake.connector
import plotly.express as px
import plotly.utils
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)


def get_conn():
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        database=os.environ.get("SNOWFLAKE_DATABASE", "PTCG_SCOUTING"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "SYSADMIN"),
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


# ---------------------------------------------------------------------------
# API endpoints (JSON, consumed by Plotly.js in templates)
# ---------------------------------------------------------------------------

@app.get("/api/search")
def search_players():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    rows = query(
        """
        SELECT player_username, player_name, country, tournaments_entered, best_placement
        FROM PTCG_SCOUTING.MARTS.PLAYERS
        WHERE lower(player_username) LIKE lower(%s)
           OR lower(player_name)     LIKE lower(%s)
        ORDER BY tournaments_entered DESC
        LIMIT 20
        """,
        (f"%{q}%", f"%{q}%"),
    )
    return jsonify(rows)


@app.get("/api/player/<username>")
def player_stats(username):
    career = query(
        "SELECT * FROM PTCG_SCOUTING.MARTS.PLAYERS WHERE player_username = %s",
        (username,),
    )
    if not career:
        return jsonify({"error": "player not found"}), 404

    history = query(
        """
        SELECT tournament_name, tournament_date, placing, player_count,
               normalized_placement, wins, losses, ties, deck_name, format
        FROM PTCG_SCOUTING.MARTS.PLAYER_TOURNAMENT_HISTORY
        WHERE player_username = %s
        ORDER BY tournament_date DESC
        """,
        (username,),
    )

    archetypes = query(
        """
        SELECT deck_name, times_played, avg_placement, win_rate, archetype_loyalty,
               first_played, last_played
        FROM PTCG_SCOUTING.MARTS.PLAYER_ARCHETYPE_HISTORY
        WHERE player_username = %s
        ORDER BY times_played DESC
        """,
        (username,),
    )

    return jsonify({"career": career[0], "history": history, "archetypes": archetypes})


@app.get("/api/player/<username>/vs/<opponent>")
def head_to_head(username, opponent):
    rows = query(
        """
        SELECT matches_played, wins, losses, ties, win_rate
        FROM PTCG_SCOUTING.MARTS.PLAYER_VS_PLAYER
        WHERE player_username = %s AND opponent_username = %s
        """,
        (username, opponent),
    )
    return jsonify(rows[0] if rows else {})


@app.get("/api/meta")
def meta_stats():
    rows = query(
        """
        SELECT deck_name,
               count(*)                                       as appearances,
               round(avg(placing), 1)                         as avg_placement,
               round(sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3) as win_rate,
               min(tournament_date)                           as first_seen,
               max(tournament_date)                           as last_seen
        FROM PTCG_SCOUTING.MARTS.PLAYER_TOURNAMENT_HISTORY
        WHERE deck_name is not null
          AND tournament_date >= dateadd('month', -3, current_date())
        GROUP BY deck_name
        ORDER BY appearances DESC
        LIMIT 30
        """
    )
    return jsonify(rows)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
