"""
Glicko-2 rating calculator for PTCG competitive players.

Reads match history from Snowflake PTCG_SCOUTING.RAW.MATCHES + STANDINGS,
processes chronologically, and writes ratings to PTCG_SCOUTING.RAW.GLICKO_RATINGS.

Glicko-2 paper: http://www.glicko.net/glicko/glicko2.pdf

Usage:
    python ingest/glicko.py          # compute ratings for all players
    python ingest/glicko.py --dry-run  # print top 20 ratings, no DB write
"""
import argparse
import logging
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import snowflake.connector
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Glicko-2 constants
# ---------------------------------------------------------------------------
MU_0 = 1500.0       # initial rating (displayed scale)
PHI_0 = 350.0       # initial rating deviation (high uncertainty for new players)
SIGMA_0 = 0.06      # initial volatility
TAU = 0.5           # system constant — constrains volatility change per period
                    # lower = more stable ratings; 0.3–1.2 is typical range
EPSILON = 0.000001  # convergence threshold for volatility iteration

# Glicko-2 internal scale
_SCALE = 173.7178


@dataclass
class Player:
    username: str
    mu: float = MU_0
    phi: float = PHI_0
    sigma: float = SIGMA_0
    # track how many rating periods with no games (for RD inflation)
    inactive_periods: int = 0
    games_played: int = 0

    # internal Glicko-2 scale
    @property
    def _mu(self) -> float:
        return (self.mu - 1500) / _SCALE

    @property
    def _phi(self) -> float:
        return self.phi / _SCALE


def _g(phi: float) -> float:
    return 1 / math.sqrt(1 + 3 * phi**2 / math.pi**2)


def _E(mu: float, mu_j: float, phi_j: float) -> float:
    return 1 / (1 + math.exp(-_g(phi_j) * (mu - mu_j)))


def update_player(player: Player, outcomes: list[tuple["Player", float]]) -> Player:
    """
    Update a player's Glicko-2 rating given a list of (opponent, score) pairs.
    score = 1.0 for win, 0.5 for tie, 0.0 for loss.
    Returns a new Player with updated values (original is unchanged).
    """
    if not outcomes:
        # no games this period — inflate RD (uncertainty grows with inactivity)
        new_phi = math.sqrt(player._phi**2 + player.sigma**2)
        return Player(
            username=player.username,
            mu=player.mu,
            phi=new_phi * _SCALE,
            sigma=player.sigma,
            inactive_periods=player.inactive_periods + 1,
            games_played=player.games_played,
        )

    mu = player._mu
    phi = player._phi

    # step 3: compute v (estimated variance)
    v_inv = sum(
        _g(opp._phi)**2 * _E(mu, opp._mu, opp._phi) * (1 - _E(mu, opp._mu, opp._phi))
        for opp, _ in outcomes
    )
    v = 1 / v_inv

    # step 4: compute delta (estimated improvement)
    delta = v * sum(
        _g(opp._phi) * (score - _E(mu, opp._mu, opp._phi))
        for opp, score in outcomes
    )

    # step 5: update volatility via Illinois algorithm
    a = math.log(player.sigma**2)
    phi_sq = phi**2

    def f(x: float) -> float:
        ex = math.exp(x)
        d2 = phi_sq + v + ex
        return (
            ex * (delta**2 - phi_sq - v - ex) / (2 * d2**2)
            - (x - a) / TAU**2
        )

    A = a
    if delta**2 > phi_sq + v:
        B = math.log(delta**2 - phi_sq - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        B = a - k * TAU

    fA, fB = f(A), f(B)
    iterations = 0
    while abs(B - A) > EPSILON and iterations < 100:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0:
            A, fA = B, fB
        else:
            fA /= 2
        B, fB = C, fC
        iterations += 1

    new_sigma = math.exp(A / 2)

    # step 6: update phi*
    phi_star = math.sqrt(phi**2 + new_sigma**2)

    # step 7: update rating and RD
    new_phi = 1 / math.sqrt(1 / phi_star**2 + 1 / v)
    new_mu = mu + new_phi**2 * sum(
        _g(opp._phi) * (score - _E(mu, opp._mu, opp._phi))
        for opp, score in outcomes
    )

    return Player(
        username=player.username,
        mu=new_mu * _SCALE + 1500,
        phi=new_phi * _SCALE,
        sigma=new_sigma,
        inactive_periods=0,
        games_played=player.games_played + len(outcomes),
    )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_matches(cur) -> list[dict]:
    """Load all matches ordered by tournament date, filtered to real head-to-head."""
    cur.execute("""
        SELECT
            m.tournament_id,
            t.tournament_date,
            m.round,
            m.player1,
            m.player2,
            m.winner
        FROM PTCG_SCOUTING.RAW.MATCHES m
        JOIN PTCG_SCOUTING.RAW.TOURNAMENTS t ON m.tournament_id = t.id
        WHERE m.player2 IS NOT NULL
          AND m.winner NOT IN ('-1', 'null')
          AND m.winner IS NOT NULL
        ORDER BY t.tournament_date, m.round
    """)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def compute_ratings(matches: list[dict]) -> dict[str, Player]:
    players: dict[str, Player] = {}

    def get_player(username: str) -> Player:
        if username not in players:
            players[username] = Player(username=username)
        return players[username]

    # group matches into rating periods by tournament
    by_tournament: dict[str, list[dict]] = defaultdict(list)
    for m in matches:
        by_tournament[m["tournament_id"]].append(m)

    # process one tournament at a time as a rating period
    for tid, t_matches in by_tournament.items():
        period_outcomes: dict[str, list[tuple[Player, float]]] = defaultdict(list)

        for m in t_matches:
            p1 = get_player(m["player1"])
            p2 = get_player(m["player2"])
            winner = m["winner"]

            if winner == m["player1"]:
                s1, s2 = 1.0, 0.0
            elif winner == m["player2"]:
                s1, s2 = 0.0, 1.0
            elif winner == "0":
                s1, s2 = 0.5, 0.5
            else:
                continue

            period_outcomes[m["player1"]].append((p2, s1))
            period_outcomes[m["player2"]].append((p1, s2))

        # update all players who played this period
        updated = {}
        for username, outcomes in period_outcomes.items():
            updated[username] = update_player(get_player(username), outcomes)

        players.update(updated)

    return players


def write_ratings(cur, players: dict[str, Player]):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PTCG_SCOUTING.RAW.GLICKO_RATINGS (
            PLAYER_USERNAME  STRING NOT NULL,
            RATING           FLOAT,
            RATING_DEVIATION FLOAT,
            VOLATILITY       FLOAT,
            RATING_LOW       FLOAT,
            RATING_HIGH      FLOAT,
            GAMES_PLAYED     INTEGER,
            _LOADED_AT       TIMESTAMP_TZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)
    cur.execute("TRUNCATE TABLE PTCG_SCOUTING.RAW.GLICKO_RATINGS")

    rows = [
        (
            p.username,
            round(p.mu, 1),
            round(p.phi, 1),
            round(p.sigma, 4),
            round(p.mu - 2 * p.phi, 1),   # ~95% confidence lower bound
            round(p.mu + 2 * p.phi, 1),   # ~95% confidence upper bound
            p.games_played,
        )
        for p in players.values()
        if p.games_played >= 3  # exclude players with too few games for meaningful rating
    ]

    cur.executemany(
        """
        INSERT INTO PTCG_SCOUTING.RAW.GLICKO_RATINGS
          (PLAYER_USERNAME, RATING, RATING_DEVIATION, VOLATILITY,
           RATING_LOW, RATING_HIGH, GAMES_PLAYED)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    log.info(f"Wrote {len(rows)} player ratings")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        database="PTCG_SCOUTING",
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
        role=os.environ.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
    )
    cur = conn.cursor()

    try:
        log.info("Loading matches...")
        matches = load_matches(cur)
        log.info(f"  {len(matches)} matches loaded")

        log.info("Computing Glicko-2 ratings...")
        players = compute_ratings(matches)
        log.info(f"  {len(players)} players rated")

        if args.dry_run:
            ranked = sorted(players.values(), key=lambda p: p.mu - p.phi, reverse=True)
            print(f"\n{'Rank':<5} {'Username':<20} {'Rating':<8} {'±RD':<8} {'σ':<8} {'Games'}")
            print("-" * 60)
            for i, p in enumerate(ranked[:20], 1):
                print(f"{i:<5} {p.username:<20} {p.mu:<8.0f} {p.phi:<8.0f} {p.sigma:<8.4f} {p.games_played}")
            return

        write_ratings(cur, players)
        conn.commit()
        log.info("Done")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
