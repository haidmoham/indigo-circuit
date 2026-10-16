"""
Polite HTTP for scraping Limitless Labs.

  - one shared requests.Session with an honest User-Agent that names the site
  - ~1 request/second with jitter (process-wide)
  - exponential backoff on 429 / 5xx, honouring Retry-After
  - on-disk cache keyed by URL; revalidates with ETag / If-Modified-Since
  - `immutable=True` serves a cached body with NO network call at all
    (used for pages of finished tournaments, which never change)

Pure stdlib + requests; importable by tests with a fake session and clock.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "IndigoCircuitBot/1.0 (+https://indigocircuit.app; competitive TCG stats; polite: ~1 req/s)",
)
DEFAULT_CACHE_DIR = os.environ.get(
    "HTTP_CACHE_DIR",
    str(Path(os.environ.get("DUCKDB_PATH", Path(__file__).parent.parent / "data" / "x")).parent / "http_cache"),
)


class CachedResponse:
    """Minimal response object shared by network and cache hits."""

    def __init__(self, url: str, status_code: int, content: bytes, headers: dict, from_cache: bool):
        self.url = url
        self.status_code = status_code
        self.content = content
        self.headers = headers
        self.from_cache = from_cache

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for {self.url}")


class PoliteSession:
    def __init__(self, cache_dir: Optional[str] = DEFAULT_CACHE_DIR, min_interval: float = 1.0,
                 jitter: float = 0.35, max_retries: int = 5, session=None,
                 sleep=time.sleep, clock=time.monotonic, rng=random.random):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT,
                                     "Accept": "text/html,application/xhtml+xml"})
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.jitter = jitter
        self.max_retries = max_retries
        self._sleep, self._clock, self._rng = sleep, clock, rng
        self._last = None
        self._lock = threading.Lock()
        self.stats = {"network": 0, "cache_hit": 0, "not_modified": 0, "retries": 0}

    # -- cache ----------------------------------------------------------------
    def _paths(self, url: str):
        h = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self.cache_dir / f"{h}.body", self.cache_dir / f"{h}.json"

    def _load(self, url: str):
        if not self.cache_dir:
            return None
        body, meta = self._paths(url)
        if body.exists() and meta.exists():
            try:
                return body.read_bytes(), json.loads(meta.read_text())
            except Exception:
                return None
        return None

    def _store(self, url: str, content: bytes, headers):
        if not self.cache_dir:
            return
        body, meta = self._paths(url)
        tmp = body.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, body)
        meta.write_text(json.dumps({
            "url": url, "etag": headers.get("ETag"), "last_modified": headers.get("Last-Modified"),
            "stored_at": time.time()}))

    # -- throttle -------------------------------------------------------------
    def _throttle(self):
        with self._lock:
            now = self._clock()
            wait = 0.0
            if self._last is not None:
                target = self.min_interval * (1 + self.jitter * (self._rng() * 2 - 1))
                wait = max(0.0, target - (now - self._last))
            if wait:
                self._sleep(wait)
            self._last = self._clock()

    # -- public -----------------------------------------------------------------
    def get(self, url: str, immutable: bool = False, timeout: int = 30) -> CachedResponse:
        cached = self._load(url)
        if cached and immutable:
            self.stats["cache_hit"] += 1
            return CachedResponse(url, 200, cached[0], {}, True)
        headers = {}
        if cached:
            if cached[1].get("etag"):
                headers["If-None-Match"] = cached[1]["etag"]
            if cached[1].get("last_modified"):
                headers["If-Modified-Since"] = cached[1]["last_modified"]

        delay = 2.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                r = self.session.get(url, headers=headers, timeout=timeout)
            except requests.RequestException as e:
                if attempt >= self.max_retries:
                    raise
                log.warning(f"  network error {e}; retry in {delay:.0f}s")
                self.stats["retries"] += 1
                self._sleep(delay)
                delay = min(delay * 2, 120)
                continue
            self.stats["network"] += 1
            if r.status_code == 304 and cached:
                self.stats["not_modified"] += 1
                return CachedResponse(url, 200, cached[0], dict(r.headers), True)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt >= self.max_retries:
                    return CachedResponse(url, r.status_code, r.content, dict(r.headers), False)
                ra = r.headers.get("Retry-After")
                wait = float(ra) if ra and ra.isdigit() else delay
                log.warning(f"  {r.status_code} from {url}; backing off {wait:.0f}s")
                self.stats["retries"] += 1
                self._sleep(wait)
                delay = min(delay * 2, 120)
                continue
            if r.status_code == 200:
                self._store(url, r.content, r.headers)
            return CachedResponse(url, r.status_code, r.content, dict(r.headers), False)
        raise RuntimeError("unreachable")


_shared: Optional[PoliteSession] = None


def shared_session() -> PoliteSession:
    global _shared
    if _shared is None:
        _shared = PoliteSession()
    return _shared
