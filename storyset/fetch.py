from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass
class Politeness:
    delay: float = 2.0
    jitter: float = 1.0
    timeout: float = 20.0
    retries: int = 3
    backoff: float = 2.0


class Fetcher:
    def __init__(self, politeness: Politeness) -> None:
        self.p = politeness
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def get_json(self, url: str) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.p.retries + 1):
            self._wait()
            try:
                resp = self.session.get(url, timeout=self.p.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                wait = self.p.backoff**attempt
                log.warning("Request failed (%s) attempt %d: %s; retrying in %.1fs", url, attempt + 1, exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                wait = self.p.backoff**attempt
                last_exc = RuntimeError(f"HTTP 429: {url}")
                log.warning("HTTP 429 on %s; retrying in %.1fs", url, wait)
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {url}")
                wait = self.p.backoff**attempt
                log.warning("HTTP %d on %s; retrying in %.1fs", resp.status_code, url, wait)
                time.sleep(wait)
                continue

            try:
                resp.raise_for_status()
                return resp.json()
            except ValueError as exc:
                raise RuntimeError(f"Invalid JSON from {url}: {exc}") from exc
            except requests.RequestException as exc:
                last_exc = exc
                break

        raise last_exc or RuntimeError(f"Unable to fetch JSON from {url}")

    def get_text(self, url: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.p.retries + 1):
            self._wait()
            try:
                resp = self.session.get(
                    url,
                    timeout=self.p.timeout,
                    headers={"Accept": "image/svg+xml,image/*,*/*;q=0.8"},
                )
            except requests.RequestException as exc:
                last_exc = exc
                wait = self.p.backoff**attempt
                log.warning("Request failed (%s) attempt %d: %s; retrying in %.1fs", url, attempt + 1, exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                wait = self.p.backoff**attempt
                last_exc = RuntimeError(f"HTTP 429: {url}")
                log.warning("HTTP 429 on %s; retrying in %.1fs", url, wait)
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {url}")
                wait = self.p.backoff**attempt
                log.warning("HTTP %d on %s; retrying in %.1fs", resp.status_code, url, wait)
                time.sleep(wait)
                continue

            try:
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as exc:
                last_exc = exc
                break

        raise last_exc or RuntimeError(f"Unable to fetch text from {url}")

    def _wait(self) -> None:
        if self.p.delay <= 0 and self.p.jitter <= 0:
            return
        gap = self.p.delay + random.uniform(0.0, self.p.jitter)
        time.sleep(gap)
