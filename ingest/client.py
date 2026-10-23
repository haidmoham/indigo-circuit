import time
import logging
import re
import requests
from typing import Optional

BASE_URL = "https://play.limitlesstcg.com/api"


class LimitlessClient:
    def __init__(self, api_key: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._next_request_at = 0.0
        self._request_interval = 0.0
        if api_key:
            self.session.headers.update({"X-Access-Key": api_key})

    def _get(self, path: str, params: dict = None):
        for attempt in range(3):
            while self._next_request_at > time.monotonic():
                time.sleep(min(30, max(0, self._next_request_at - time.monotonic())))
            resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=30)
            # The public API now uses the standard RateLimit headers. Pace
            # requests across its advertised window, including cached results.
            policy = resp.headers.get("RateLimit-Policy", "")
            quota, window = re.search(r"\bq=(\d+)", policy), re.search(r"\bw=(\d+)", policy)
            if quota and window and int(quota[1]) > 0:
                self._request_interval = int(window[1]) / int(quota[1]) + 0.1
            delay = self._request_interval
            if resp.status_code == 429 and attempt < 2:
                delay = max(delay, float(resp.headers.get("Retry-After", 60)))
                logging.getLogger(__name__).warning("Public API rate limit; resuming after %.0f seconds", delay)
                self._next_request_at = time.monotonic() + delay
                continue
            resp.raise_for_status()
            rate = resp.headers.get("RateLimit", "")
            remaining, reset = re.search(r"\br=(\d+)", rate), re.search(r"\bt=(\d+)", rate)
            if remaining and reset and int(remaining[1]) == 0:
                delay = max(delay, int(reset[1]) + 1)
            legacy_remaining = resp.headers.get("X-RateLimit-Remaining")
            if legacy_remaining and int(legacy_remaining) < 10:
                delay = max(delay, 2)
            self._next_request_at = time.monotonic() + delay
            return resp.json()

    def get_tournaments(self, game: str = "PTCG", limit: int = 100, page: int = 1) -> list:
        return self._get("/tournaments", params={"game": game, "limit": limit, "page": page})

    def get_tournament_details(self, tournament_id: str) -> dict:
        return self._get(f"/tournaments/{tournament_id}/details")

    def get_standings(self, tournament_id: str) -> list:
        return self._get(f"/tournaments/{tournament_id}/standings")

    def get_pairings(self, tournament_id: str) -> list:
        return self._get(f"/tournaments/{tournament_id}/pairings")
