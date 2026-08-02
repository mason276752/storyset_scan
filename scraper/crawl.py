"""爬取主流程：JSON 分頁翻頁 → 每頁 item 抽欄位 → 量測 SVG 尺寸 → 寫入 SQLite。

storyset 的資料來源是 JSON API：每個分頁就是一份「一頁多圖、每張直接帶全部欄位」
的清單，沒有「詳細頁」這層。對應 irasutoya 的 listing.fields_on_listing 模式 ——
不同的是結構是 JSON 不是 HTML，所以抽取寫在這裡，不走 selector 設定檔。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterator

from dateutil import parser as dateparser

from .config import SiteConfig
from .db import Database, ImageRecord
from .fetch import Fetcher, TimeBudgetExceeded

log = logging.getLogger(__name__)


class _Budget(Exception):
    """達到筆數上限或時間上限時，用來乾淨地中止整趟爬取（進度已存好）。"""


# 列表頁偶爾會抽不到項目（API 殘缺、站方臨時異常）。只有連續這麼多頁都「全部已抓過」，
# 才認定是走到盡頭而停下來；否則單一異常頁會讓游標永遠卡在那裡。
EMPTY_LISTING_TOLERANCE = 3


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_date(value: str | None) -> str | None:
    """回傳 ISO 8601；只有日期就回 YYYY-MM-DD。"""
    if not value:
        return None
    try:
        dt = dateparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.hour or dt.minute or dt.second:
        return dt.isoformat()
    return dt.date().isoformat()


class Crawler:
    def __init__(
        self,
        cfg: SiteConfig,
        db: Database,
        fetcher: Fetcher | None = None,
        limit: int = 0,
        force: bool = False,
        dry_run: bool = False,
        max_runtime: float = 0.0,
        restart: bool = False,
    ):
        self.cfg = cfg
        self.db = db
        self.fetcher = fetcher or Fetcher(cfg.politeness)
        self.limit = limit or cfg.max_items
        self.force = force
        self.dry_run = dry_run
        self.restart = restart
        self.deadline = time.monotonic() + max_runtime if max_runtime else None
        # 讓取得層在等待 429 冷卻時也看得到收工時間
        self.fetcher.deadline = self.deadline
        self.saved = 0
        self.skipped = 0
        self.errors = 0
        self.failed: list[tuple[str, str]] = []  # 本次失敗的 (url, 原因)
        self.empty: list[str] = []  # 本次沒有圖片的分頁

    # ---------- 對外 ----------

    def run(self) -> None:
        try:
            for start in self.cfg.start_urls:
                self._crawl_listing(start)
        except (_Budget, TimeBudgetExceeded) as exc:
            log.info("%s，進度已保存，下次執行會從中斷處繼續", exc)
        except KeyboardInterrupt:
            log.warning("使用者中斷，保存已抓到的資料")
        finally:
            if not self.dry_run:
                self.db.commit()
        self._report()

    def crawl_urls(self, urls: list[str]) -> None:
        """直接處理指定的分頁清單（重試失敗頁面用）。"""
        try:
            for url in urls:
                self._crawl_listing_page(url)
        except (_Budget, TimeBudgetExceeded) as exc:
            log.info("%s", exc)
        except KeyboardInterrupt:
            log.warning("使用者中斷")
        finally:
            if not self.dry_run:
                self.db.commit()
        self._report()

    def _report(self) -> None:
        log.info(
            "完成：新增/更新 %d 筆｜跳過 %d 頁（已抓過）｜無圖片 %d 頁｜失敗 %d 頁",
            self.saved, self.skipped, len(self.empty), self.errors,
        )
        if self.empty:
            log.info("本次沒有圖片的分頁（記為 empty，不會重抓）：")
            for url in self.empty:
                log.info("    - %s", url)
        if self.failed:
            log.warning("本次失敗的分頁（記為 error，下次會重試）：")
            for url, reason in self.failed:
                log.warning("    - %s\n        原因：%s", url, reason)

        if self.dry_run:
            return

        # 游標只會往前走，越過的分頁不會再訪問，所以待處理的頁面必須靠 retry 補。
        pending_error = len(self.db.pages_by_status("error", self.cfg.name))
        pending_empty = len(self.db.pages_by_status("empty", self.cfg.name))
        if pending_error:
            log.warning(
                "資料庫累積 %d 個 error 分頁，用 `retry` 補：\n"
                "    python -m scraper retry -c <設定檔> -d <資料庫>",
                pending_error,
            )
        missing_size = len(self.db.images_without_size(self.cfg.name))
        if missing_size:
            log.warning(
                "有 %d 張圖沒量到尺寸（量測當下可能被 429 擋掉），用 `remeasure` 補：\n"
                "    python -m scraper remeasure -c <設定檔> -d <資料庫>",
                missing_size,
            )
        if pending_empty:
            log.info(
                "資料庫累積 %d 個 empty 分頁（抓過但沒有圖）。多數是空頁之類，"
                "但 API 變動或回應殘缺也會落到這裡，偶爾用 `retry --status empty` 複查",
                pending_empty,
            )

    # ---------- 分頁 ----------

    def _check_budget(self) -> None:
        if self.deadline and time.monotonic() >= self.deadline:
            raise _Budget("已達時間上限")

    def _listing_pages(self, start_url: str) -> Iterator[tuple[str, dict]]:
        """跟著 JSON 的 links.next 翻頁，回傳 (page_url, data)。"""
        cursor = None if self.restart else self.db.get_cursor(self.cfg.name, start_url)
        url: str | None = cursor or start_url
        if cursor:
            log.info("從上次中斷的分頁繼續：%s", cursor)
        seen: set[str] = set()
        while url:
            self._check_budget()
            if url in seen:  # next 指回自己就停
                return
            seen.add(url)
            data = self._get_json(url)
            if data is None:
                return
            yield url, data
            # 這一頁都處理完了才推進游標
            self._save_cursor(start_url, url)
            url = self._next_page(data)

    def _save_cursor(self, start_url: str, value: str) -> None:
        if self.dry_run:
            return
        self.db.set_cursor(self.cfg.name, start_url, value)
        self.db.commit()

    def _next_page(self, data: dict) -> str | None:
        links = data.get("links")
        if isinstance(links, dict):
            nxt = links.get("next")
            if isinstance(nxt, str) and nxt.strip():
                if self.cfg.in_scope(nxt):
                    return nxt.strip()
                log.debug("next 網址不在 allowed_domains 內，略過：%s", nxt)
        return None

    # ---------- 抽取 ----------

    def _extract_items(self, data: dict, page_url: str) -> list[ImageRecord]:
        """一份 JSON 分頁 → ImageRecord 清單。"""
        items = data.get("data")
        if not isinstance(items, list):
            return []
        records: list[ImageRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            image_url = _as_text(item.get("src"))
            if not image_url:
                continue
            if not image_url.lower().startswith(("http://", "https://")):
                log.warning("略過非 http 圖片網址：%s", image_url)
                continue
            records.append(
                ImageRecord(
                    site=self.cfg.name,
                    page_url=_as_text(item.get("url")) or page_url,
                    image_url=image_url,
                    name=self._extract_name(item),
                    description=None,  # storyset 不採集 description（刻意取捨）
                    published_at=_to_date(item.get("published_at")),
                    tags=self._extract_tags(item, page_url),
                )
            )
        return records

    def _extract_name(self, item: dict) -> str | None:
        illustration = item.get("illustration")
        if isinstance(illustration, dict):
            name = illustration.get("name") or illustration.get("slug")
            if isinstance(name, str) and name.strip():
                return name.strip()
        name = item.get("slug")
        return name if isinstance(name, str) and name.strip() else None

    def _extract_tags(self, item: dict, page_url: str) -> list[str]:
        raw = item.get("tags")
        names: list[str] = []
        if isinstance(raw, list):
            for tag in raw:
                if isinstance(tag, dict) and tag.get("name"):
                    names.append(str(tag["name"]).strip())
        return self._normalize_tags(names)

    # ---------- 存檔 ----------

    def _crawl_listing(self, start_url: str) -> None:
        blank_streak = 0  # 連續幾頁「整頁都已抓過」

        for page_url, data in self._listing_pages(start_url):
            records = self._extract_items(data, page_url)
            if not records:
                # 真的空頁（API 沒回 data）：記 empty，不重抓
                self.empty.append(page_url)
                self._mark(page_url, "empty")
                log.info("分頁沒有項目，記為 empty：%s", page_url)
                continue

            # 分流：哪些是新增、哪些已在 db
            pending: list[ImageRecord] = []
            done_here = 0
            for rec in records:
                if not self.force and self.db.is_done(rec.image_url):
                    done_here += 1
                else:
                    pending.append(rec)
            self.skipped += done_here
            self._process_page(page_url, records, pending, done_here)

            # 整頁都已經抓過 → 計入連續空白；連續多頁就認定走到盡頭
            if not pending:
                blank_streak += 1
                if blank_streak >= EMPTY_LISTING_TOLERANCE:
                    log.warning(
                        "連續 %d 頁全部已抓過，停止翻頁：%s",
                        blank_streak, page_url,
                    )
                    return
            else:
                blank_streak = 0

    def _crawl_listing_page(self, url: str) -> None:
        """單一分頁（retry 用）。"""
        self._check_budget()
        data = self._get_json(url)
        if data is None:
            return
        records = self._extract_items(data, url)
        if not records:
            self.empty.append(url)
            self._mark(url, "empty")
            log.info("分頁沒有項目，記為 empty：%s", url)
            return

        # retry 時 force 已在外頭設定，這裡一律當成要處理
        pending = [rec for rec in records if self.force or not self.db.is_done(rec.image_url)]
        self.skipped += len(records) - len(pending)
        self._process_page(url, records, pending, len(records) - len(pending))

    def _process_page(
        self, page_url: str, records: list[ImageRecord],
        pending: list[ImageRecord], done_here: int,
    ) -> None:
        if pending:
            log.info(
                "分頁 %s → %d 個項目（%d 個要爬，%d 個已抓過）",
                page_url, len(records), len(pending), done_here,
            )
        else:
            log.info("分頁 %s → %d 個項目全部抓過，直接翻下一頁", page_url, len(records))

        if not pending:
            return

        # 跨項目收集待量測的圖 URL，保序去重後一次並行打
        need = self._collect_to_measure(pending, page_url)
        sizes = self.fetcher.image_sizes(need, referer=page_url) if need else {}
        if need:
            ok = sum(1 for v in sizes.values() if v)
            log.debug("量測 %d 張 SVG（成功 %d）：%s", len(need), ok, page_url)

        got = 0
        for rec in pending:
            size = sizes.get(rec.image_url)
            if size:
                rec.width, rec.height = size
            if self._persist(page_url, rec):
                got += 1

        if not self.dry_run:
            self._mark(page_url, "done" if got else "empty")
            if self.saved % 20 == 0:
                self.db.commit()

    def _collect_to_measure(self, records: list[ImageRecord], page_url: str) -> list[str]:
        mode = self.cfg.measure_size
        if mode == "never":
            return []
        urls = []
        for rec in records:
            need = mode == "always" or (mode == "missing" and not (rec.width and rec.height))
            if need:
                urls.append(rec.image_url)
        return list(dict.fromkeys(urls))

    def _persist(self, page_url: str, rec: ImageRecord) -> bool:
        """寫入一筆；回傳是否真的寫入（dry-run 也算）。"""
        if self.dry_run:
            size = f"{rec.width}x{rec.height}" if rec.width and rec.height else "尺寸未知"
            log.info(
                "[dry-run] %s | %s | %s | tags=%s\n           %s",
                size, rec.name, rec.published_at, rec.tags, rec.image_url,
            )
        else:
            self.db.upsert_image(rec)

        self.saved += 1
        if self.limit and self.saved >= self.limit:
            # 這一頁還沒處理完就中斷，所以**不能**標記成 done
            # ——標記了下次就會跳過，該頁剩下的圖會永久漏掉。
            # 已寫入的圖先 commit 保住，整頁留待下次重抓（upsert 不會產生重複）。
            if not self.dry_run:
                self.db.commit()
            raise _Budget(f"已達筆數上限 {self.limit}（這一頁未完成，下次會重抓）")
        return True

    def _mark(self, url: str, status: str, error: str | None = None) -> None:
        if self.dry_run:
            return
        self.db.mark(url, self.cfg.name, status, error)
        self.db.commit()

    def _normalize_tags(self, raw_tags: list[str]) -> list[str]:
        sep = self.cfg.tag_separator
        if sep:
            pattern = "[" + re.escape(sep) + "]"
            raw_tags = [part for tag in raw_tags for part in re.split(pattern, tag)]
        return sorted({t.strip() for t in raw_tags if t and t.strip()})

    # ---------- 工具 ----------

    def _get_json(self, url: str, referer: str | None = None) -> dict | None:
        try:
            return self.fetcher.get_json(url, referer=referer)
        except TimeBudgetExceeded:
            # 收工訊號，不是這一頁的問題。要是被下面當成失敗記成 error，
            # 等於把「還沒處理」寫成「處理失敗」，進度記錄就不誠實了。
            raise
        except Exception as exc:
            self.errors += 1
            self.failed.append((url, f"{type(exc).__name__}: {exc}"))
            log.error("取得失敗 %s：%s", url, exc)
            self._mark(url, "error", str(exc))
            return None