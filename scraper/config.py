"""站台設定檔（YAML）→ dataclass。

storyset 的資料來源是 JSON API，沒有 HTML/CSS selector 可抽，因此設定檔只描述
「從哪個網址開始、怎麼限速、量不量尺寸」這類基礎設定；JSON 欄位到資料物件的
對應寫死在 crawl.py（這個站只有一個來源，硬做 jq 抽取是過度設計）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# 一般桌機 Chrome 的 User-Agent。維持與其他標頭一致很重要 ——
# UA 說是 Chrome 卻少了 Sec-Fetch-* 這類標頭，反而比誠實的爬蟲 UA 更可疑。
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 誠實表明身分的版本，把 browser_headers 設成 false 時使用
BOT_UA = "Mozilla/5.0 (compatible; StockLibraryScan/0.1) metadata-only crawler"


@dataclass
class Politeness:
    """爬蟲速度與禮貌設定。"""

    delay: float = 2.0  # JSON API 請求之間至少間隔幾秒
    jitter: float = 1.0  # 額外隨機延遲 0~jitter 秒
    # SVG 量測只讀檔頭、成本低，可以並行；JSON 分頁一律維持單執行緒序列
    image_concurrency: int = 10  # 同時最多量測幾張圖
    image_delay: float = 0.0  # 圖片請求之間的最小間隔（0 = 只靠並行數限制）
    timeout: float = 20.0
    retries: int = 3
    backoff: float = 2.0  # 5xx 的重試退避倍數
    # 被 429 擋下時暫停多久再試（秒）。這是全域冷卻，並行中的量測請求也會一起停。
    too_many_requests_wait: float = 60.0
    # 送出跟一般瀏覽器一致的標頭組合（Accept、Referer 等）。
    # 關掉的話會用 BOT_UA 並只送最基本的標頭。
    browser_headers: bool = True
    user_agent: str = ""  # 留空 = 依 browser_headers 自動選
    accept_language: str = "en,en-US;q=0.9"
    respect_robots: bool = False
    headers: dict[str, str] = field(default_factory=dict)  # 自訂標頭，優先度最高

    def __post_init__(self) -> None:
        if not self.user_agent:
            self.user_agent = CHROME_UA if self.browser_headers else BOT_UA

    @classmethod
    def parse(cls, raw: Any) -> Politeness:
        return cls(**(raw or {}))


@dataclass
class SiteConfig:
    name: str
    start_urls: list[str]
    politeness: Politeness
    allowed_domains: list[str] = field(default_factory=list)
    max_items: int = 0  # 0 = 不限
    # 圖片尺寸：always=一律連線量測，missing=JSON 沒寫才量測，never=不量測
    measure_size: str = "missing"
    # 標籤抽出來是一整串字串時，用這些字元切開（None = 不切）
    tag_separator: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> SiteConfig:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        start_urls = raw.get("start_urls") or []
        if isinstance(start_urls, str):
            start_urls = [start_urls]
        if not start_urls:
            raise ValueError(f"{path}: start_urls 不能是空的")

        allowed = raw.get("allowed_domains") or []
        if not allowed:  # 沒指定就鎖在起始網址的網域內
            allowed = sorted({h for u in start_urls if (h := urlparse(u).hostname)})

        measure = raw.get("measure_size", "missing")
        if measure not in {"always", "missing", "never"}:
            raise ValueError(f"measure_size 只能是 always / missing / never，收到 {measure!r}")

        return cls(
            name=raw.get("name") or Path(path).stem,
            start_urls=start_urls,
            politeness=Politeness.parse(raw.get("politeness")),
            allowed_domains=allowed,
            max_items=int(raw.get("max_items") or 0),
            measure_size=measure,
            tag_separator=raw.get("tag_separator"),
        )

    def in_scope(self, url: str) -> bool:
        host = urlparse(url).hostname  # 不含 port
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)