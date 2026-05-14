import time
import requests
from typing import Optional

BASE_URL = "https://play.limitlesstcg.com/api"


class LimitlessClient:
    def __init__(self, api_key: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if api_key:
            self.session.headers.update({"X-Access-Key": api_key})

    def _get(self, path: str, params: dict = None):
        resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=30)
        resp.raise_for_status()
        # back off when approaching rate limit ceiling
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining and int(remaining) < 10:
            time.sleep(2)
        return resp.json()

    def get_tournaments(self, game: str = "PTCG", limit: int = 100, page: int = 1) -> list:
        return self._get("/tournaments", params={"game": game, "limit": limit, "page": page})

    def get_tournament_details(self, tournament_id: str) -> dict:
        return self._get(f"/tournaments/{tournament_id}/details")

    def get_standings(self, tournament_id: str) -> list:
        return self._get(f"/tournaments/{tournament_id}/standings")

    def get_pairings(self, tournament_id: str) -> list:
        return self._get(f"/tournaments/{tournament_id}/pairings")
