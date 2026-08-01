from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse, urljoin

from .db import Database, ImageRecord
from .fetch import Fetcher

log = logging.getLogger(__name__)


class Crawler:
    def __init__(
        self,
        db: Database,
        fetcher: Fetcher,
        start_url: str,
        max_pages: int = 0,
        limit: int = 0,
        dry_run: bool = False,
    ) -> None:
        self.db = db
        self.fetcher = fetcher
        self.start_url = start_url
        self.max_pages = max_pages
        self.limit = limit
        self.dry_run = dry_run
        self.saved = 0
        self.errors = 0
        self.seen_image_urls: set[str] = set()

    def run(self) -> bool:
        self._load_seen_image_urls()
        page = 1
        url = self.start_url
        while url:
            if self.max_pages and page > self.max_pages:
                log.info("Reached max_pages=%d, stopping", self.max_pages)
                break

            log.info("Fetching JSON page %d: %s", page, url)
            try:
                data = self.fetcher.get_json(url)
            except Exception as exc:
                log.error("Failed to fetch JSON page %s: %s", url, exc)
                self.errors += 1
                break

            items = self._extract_items(data)
            if not items:
                log.info("No items found on page %d", page)
                break

            saved = self._save_items(items, url)
            log.info("Saved %d items from page %d", saved, page)
            if self.limit and self.saved >= self.limit:
                log.info("Reached record limit=%d", self.limit)
                break

            url = self._next_page(data)
            page += 1

        return self.errors == 0

    def _extract_items(self, data: dict[str, Any]) -> list[ImageRecord]:
        items = []
        for item in data.get("data", []):
            image_url = item.get("src")
            if not image_url:
                continue
            if image_url in self.seen_image_urls:
                log.debug("Skipping duplicated image URL: %s", image_url)
                continue
            page_url = item.get("url") or self.start_url
            tags = self._normalize_tags(item.get("tags", []))
            published_at = item.get("published_at")
            name = self._extract_name(item)
            # storyset: do not capture description/style
            items.append(
                ImageRecord(
                    site="storyset",
                    page_url=page_url,
                    image_url=image_url,
                    name=name,
                    published_at=published_at,
                    tags=tags,
                )
            )
            self.seen_image_urls.add(image_url)
        return items

    def _save_items(self, items: list[ImageRecord], page_url: str) -> int:
        self._fill_svg_dimensions_concurrently([item for item in items if self._is_svg_url(item.image_url)])
        count = 0
        for item in items:
            if self.dry_run:
                log.info(
                    "[dry-run] %s %s %s %sx%s",
                    item.image_url,
                    item.name,
                    item.tags,
                    item.width or "?",
                    item.height or "?",
                )
            else:
                self.db.upsert_image(item)
                self.db.commit()
            self.saved += 1
            count += 1
            if self.limit and self.saved >= self.limit:
                break
        return count

    def _fill_svg_dimensions_concurrently(self, items: list[ImageRecord]) -> None:
        if not items:
            return
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._fill_svg_dimensions, item): item for item in items}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    item = futures[future]
                    log.warning("SVG dimension worker failed for %s: %s", item.image_url, exc)

    def _next_page(self, data: dict[str, Any]) -> str | None:
        links = data.get("links", {})
        if isinstance(links, dict) and links.get("next"):
            next_url = links["next"]
            if isinstance(next_url, str) and next_url.strip():
                return next_url.strip()
        return None

    def _load_seen_image_urls(self) -> None:
        try:
            self.seen_image_urls = self.db.existing_image_urls()
        except Exception:
            self.seen_image_urls = set()

    def _normalize_tags(self, tags_value: Any) -> list[str]:
        raw_tags = []
        for tag in tags_value if isinstance(tags_value, list) else []:
            if isinstance(tag, dict) and tag.get("name"):
                raw_tags.append(str(tag["name"]).strip())
        return sorted({tag for tag in raw_tags if tag})

    def _extract_name(self, item: dict[str, Any]) -> str | None:
        illustration = item.get("illustration")
        if isinstance(illustration, dict):
            name = illustration.get("name") or illustration.get("slug")
            if isinstance(name, str) and name.strip():
                return name.strip()
        name = item.get("slug")
        return name if isinstance(name, str) else None

    def _is_svg_url(self, url: str) -> bool:
        return urlparse(url).path.lower().endswith(".svg")

    def _fill_svg_dimensions(self, item: ImageRecord) -> None:
        if item.width is not None and item.height is not None:
            return
        try:
            svg_text = self.fetcher.get_text(item.image_url)
        except Exception as exc:
            log.warning("Failed to fetch SVG %s: %s", item.image_url, exc)
            return

        width, height = self._parse_svg_dimensions(svg_text)
        if width is not None:
            item.width = width
        if height is not None:
            item.height = height

    def _parse_svg_dimensions(self, svg_text: str) -> tuple[int | None, int | None]:
        try:
            root = ET.fromstring(svg_text)
        except ET.ParseError:
            return None, None

        if root.tag.split("}")[-1] != "svg":
            return None, None

        width = self._parse_svg_length(root.attrib.get("width"))
        height = self._parse_svg_length(root.attrib.get("height"))
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
        return width, height

    def _parse_svg_length(self, value: str | None) -> int | None:
        if not value:
            return None
        value = value.strip()
        match = re.match(r"^([0-9]+(?:\.[0-9]+)?)(?:px)?$", value)
        if not match:
            return None
        try:
            return int(round(float(match.group(1))))
        except ValueError:
            return None
