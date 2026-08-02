"""HTTP 取得層：限速、重試、robots.txt、429 全域冷卻、SVG 量測尺寸。

JSON 分頁只走單執行緒序列；SVG 量測可並行。速度控制是這支爬蟲的硬性要求，
JSON 請求不做並發。

跟 irasutoya 版的差異只在「抽尺寸」這一步：irasutoya 抓點陣圖，用 PIL 串流讀
檔頭就夠；storyset 是 SVG，PIL 不支援，得整段下載後用 xml.etree 解析。
"""

from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from .config import Politeness

log = logging.getLogger(__name__)


class TimeBudgetExceeded(Exception):
    """等待 429 冷卻的過程中超出時間預算，必須乾淨收工而不是睡完。"""


class RateLimiter:
    """確保任兩次請求之間至少間隔 delay 秒，另加 0~jitter 的隨機抖動。

    SVG 量測是多執行緒的，所以整段等待都在鎖內，讓間隔對所有執行緒一致生效。
    delay 與 jitter 都是 0 時完全不鎖，避免白白序列化。
    """

    def __init__(self, delay: float, jitter: float = 0.0):
        self.delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.delay and not self.jitter:
            return
        with self._lock:
            import random

            gap = self.delay + random.uniform(0.0, self.jitter)
            elapsed = time.monotonic() - self._last
            if elapsed < gap:
                time.sleep(gap - elapsed)
            self._last = time.monotonic()


class Fetcher:
    def __init__(self, politeness: Politeness):
        self.p = politeness
        self.limiter = RateLimiter(politeness.delay, politeness.jitter)  # JSON 分頁
        self.image_limiter = RateLimiter(politeness.image_delay)  # SVG 量測
        self._headers = {
            "User-Agent": politeness.user_agent,
            "Accept-Language": politeness.accept_language,
            "Accept": "application/json",
            **(politeness.headers if politeness.browser_headers else {}),
        }
        self._local = threading.local()  # 每個執行緒各自的 Session
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        # 被 429 擋下時的冷卻，以 host 為單位：
        # 限流是伺服器層級的，同一台主機上的 JSON 與 SVG 必須一起停，
        # 但不該波及其他主機（例如圖片放在另一個 CDN 的情況）。
        self._cooldown: dict[str, float] = {}
        self._cooldown_lock = threading.Lock()
        # 由 Crawler 設定的收工時間點（time.monotonic 基準）。
        # 冷卻動輒一分鐘起跳，等待期間必須看得到時間上限，否則會遠遠超時。
        self.deadline: float | None = None

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            self._local.session = session
        return session

    # ---------- robots ----------

    def allowed(self, url: str) -> bool:
        if not self.p.respect_robots:
            return True
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        with self._robots_lock:  # 多執行緒量測 SVG 時，同一個 host 只載入一次
            if root not in self._robots:
                self._robots[root] = self._load_robots(root)
            rp = self._robots[root]
        if rp is None:  # 拿不到 robots.txt 就當作沒有限制
            return True
        return rp.can_fetch(self.p.user_agent, url)

    def _load_robots(self, root: str) -> RobotFileParser | None:
        url = urljoin(root, "/robots.txt")
        try:
            self.limiter.wait()
            resp = self.session.get(url, timeout=self.p.timeout)
            if resp.status_code != 200:
                return None
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            log.info("已載入 robots.txt：%s", url)
            return rp
        except requests.RequestException as exc:
            log.warning("讀不到 robots.txt（  %s  ）：%s", url, exc)
            return None

    # ---------- 429 冷卻（以 host 為單位）----------

    def set_cooldown(self, url: str, seconds: float) -> None:
        host = urlparse(url).hostname or ""
        with self._cooldown_lock:
            until = max(self._cooldown.get(host, 0.0), time.monotonic() + seconds)
            self._cooldown[host] = until

    def wait_cooldown(self, url: str) -> None:
        """這台主機若在冷卻中就等到結束。

        分段睡，好讓等待中途也能察覺時間上限已到 —— 冷卻可能長達一分鐘，
        睡完才發現超時的話，收工時間會嚴重失準。
        """
        host = urlparse(url).hostname or ""
        while True:
            with self._cooldown_lock:
                until = self._cooldown.get(host, 0.0)
            remain = until - time.monotonic()
            if remain <= 0:
                return
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise TimeBudgetExceeded(
                    f"等待 {host} 的 429 冷卻（還要 {remain:.0f} 秒）期間已達時間上限"
                )
            time.sleep(min(remain, 1.0))

    # ---------- 請求 ----------

    def _request(
        self, method: str, url: str, limiter: RateLimiter | None = None,
        accept: str | None = None, **kwargs,
    ) -> requests.Response:
        """帶重試；429 觸發全域冷卻，5xx 用指數退避。兩者都尊重 Retry-After。"""
        limiter = limiter or self.limiter
        # JSON 分頁預設 application/json；SVG 量測由呼叫端傳 image/svg+xml,...
        if accept is None:
            accept = "application/json"
        kwargs.setdefault("headers", {})["Accept"] = accept
        last_exc: Exception | None = None
        for attempt in range(self.p.retries + 1):
            self.wait_cooldown(url)  # 這台主機還在 429 冷卻中就先等
            limiter.wait()
            try:
                resp = self.session.request(method, url, timeout=self.p.timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                wait = self.p.backoff**attempt
                log.warning("請求失敗（  %s  ）第 %d 次：%s，%.1fs 後重試", url, attempt + 1, exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                server_wait = float(retry_after) if (retry_after or "").isdigit() else 0.0
                last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)

                if resp.status_code == 429:
                    # 被限流：讓整台主機冷卻，JSON 與並行中的 SVG 請求都會停下來。
                    # 冷卻要在放棄重試之前就設好 —— 這一個請求就算不再重試，
                    # 其他執行緒和後續請求仍然必須受到保護。
                    wait = max(server_wait, self.p.too_many_requests_wait)
                    self.set_cooldown(url, wait)
                    log.warning(
                        "HTTP 429（ %s  ），%s 全站暫停 %.0f 秒",
                        url, urlparse(url).hostname, wait,
                    )
                    if attempt >= self.p.retries:
                        break
                    continue

                if attempt >= self.p.retries:
                    break
                wait = server_wait or self.p.backoff**attempt
                log.warning("HTTP %d（ %s  ），%.1fs 後重試", resp.status_code, url, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        raise last_exc or RuntimeError(f"無法取得 {url}")

    def _extra_headers(self, url: str, referer: str | None) -> dict[str, str | None]:
        headers: dict[str, str | None] = {}
        if self.p.browser_headers:
            headers["Sec-Fetch-Site"] = (
                "none" if not referer
                else "same-origin" if urlparse(url).hostname == urlparse(referer).hostname
                else "cross-site"
            )
        if referer:
            headers["Referer"] = referer
        return headers

    def get_json(self, url: str, referer: str | None = None) -> dict:
        """取得單一 JSON 分頁。"""
        if not self.allowed(url):
            raise PermissionError(f"robots.txt 不允許抓取：{url}")
        resp = self._request("GET", url, headers=self._extra_headers(url, referer))
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"無法解析回應為 JSON（{url}）：{exc}") from exc

    # ---------- SVG 尺寸 ----------

    def image_sizes(
        self, urls: list[str], referer: str | None = None
    ) -> dict[str, tuple[int, int] | None]:
        """並行量測多張 SVG 的尺寸（同時最多 image_concurrency 張）。

        只有圖片走並行；JSON 分頁一律由呼叫端序列取得。
        """
        workers = min(self.p.image_concurrency, len(urls))
        if workers <= 1:
            return {url: self.svg_size(url, referer) for url in urls}

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="svgsize") as pool:
            results = pool.map(lambda u: self.svg_size(u, referer), urls)
            return dict(zip(urls, results))

    def svg_size(self, url: str, referer: str | None = None) -> tuple[int, int] | None:
        """下載整段 SVG 文字後解析 width/height（或從 viewBox 推算）。

        PIL 不支援 SVG，所以無法像點陣圖那樣只讀檔頭；SVG 檔通常不大，整份下載。
        """
        if not self.allowed(url):
            log.warning("robots.txt 不允許量測圖片：%s", url)
            return None
        headers = self._extra_headers(url, referer)
        headers["Sec-Fetch-Dest"] = "image"
        headers["Sec-Fetch-Mode"] = "no-cors"
        try:
            resp = self._request(
                "GET", url, limiter=self.image_limiter,
                accept="image/svg+xml,image/*,*/*;q=0.8",
                headers=headers,
            )
        except TimeBudgetExceeded:
            raise  # 收工訊號不能被當成「這張圖量不到」吞掉
        except Exception as exc:
            log.warning("量測尺寸失敗（  %s  ）：%s", url, exc)
            return None

        try:
            text = resp.text
        except Exception as exc:
            log.warning("讀取 SVG 內容失敗（  %s  ）：%s", url, exc)
            return None

        return _parse_svg_dimensions(text, url)


# SVG 的 width/height 可能是 "100"、"100px"、"100.0"；viewBox 是 "0 0 100 100"
_LENGTH_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(?:px)?$")


def _parse_svg_length(value: str | None) -> int | None:
    if not value:
        return None
    m = _LENGTH_RE.match(value.strip())
    if not m:
        return None
    try:
        return int(round(float(m.group(1))))
    except ValueError:
        return None


def _parse_svg_dimensions(svg_text: str, url: str) -> tuple[int, int] | None:
    """從 <svg> 的 width/height 屬性或 viewBox 推算尺寸。

    缺一邊時用 viewBox 的寬高比補上；兩邊都缺就以 viewBox 為準。
    """
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        log.warning("SVG 解析失敗（  %s  ）", url)
        return None

    if root.tag.split("}")[-1] != "svg":
        log.warning("根元素不是 <svg>（  %s  ）", url)
        return None

    width = _parse_svg_length(root.attrib.get("width"))
    height = _parse_svg_length(root.attrib.get("height"))
    viewbox = root.attrib.get("viewBox")
    if viewbox:
        parts = re.split(r"[\s,]+", viewbox.strip())
        if len(parts) == 4:
            try:
                vb_w = float(parts[2])
                vb_h = float(parts[3])
                if width is None and height is not None and vb_w and vb_h:
                    width = int(round(height * (vb_w / vb_h)))
                elif height is None and width is not None and vb_w and vb_h:
                    height = int(round(width * (vb_h / vb_w)))
                elif width is None and height is None:
                    width = int(round(vb_w))
                    height = int(round(vb_h))
            except ValueError:
                pass

    if width is None or height is None:
        return None
    return width, height