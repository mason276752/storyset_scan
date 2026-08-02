"""SQLite 儲存層。

一張圖一筆，以 image_url 為唯一鍵。同一個圖檔常出現在多個來源頁裡，各頁給的
名稱與說明不同，所以「名稱／說明／來源頁」跟標籤一樣是多值的，掛在 image_sources。

    images         一張圖一筆（尺寸、最早公開日期）
    image_sources  這張圖在每個來源頁的名稱與說明
    tags/image_tags 標籤（多對多，天然無序不重複）

名稱與說明另外掛 FTS5 全文索引（優先用 trigram 分詞，對中日文子字串搜尋友善）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id           INTEGER PRIMARY KEY,
    site         TEXT    NOT NULL,
    image_url    TEXT    NOT NULL UNIQUE,
    width        INTEGER,
    height       INTEGER,
    published_at TEXT,               -- 各來源頁裡最早的公开日期
    fetched_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_site      ON images (site);
CREATE INDEX IF NOT EXISTS idx_images_published ON images (published_at);

-- 同一張圖可能出現在多個來源頁，各頁給的名稱／說明不同，全部保留
CREATE TABLE IF NOT EXISTS image_sources (
    image_id     INTEGER NOT NULL REFERENCES images (id) ON DELETE CASCADE,
    page_url     TEXT    NOT NULL,
    name         TEXT,
    description  TEXT,
    published_at TEXT,
    PRIMARY KEY (image_id, page_url)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_image_sources_name ON image_sources (name);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS image_tags (
    image_id INTEGER NOT NULL REFERENCES images (id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags (id)   ON DELETE CASCADE,
    PRIMARY KEY (image_id, tag_id)
) WITHOUT ROWID;
-- 反向索引：由標籤找圖
CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags (tag_id, image_id);

-- 續爬用：記錄每個頁面的處理狀態
CREATE TABLE IF NOT EXISTS crawl_state (
    url        TEXT PRIMARY KEY,
    site       TEXT NOT NULL,
    status     TEXT NOT NULL,        -- done | empty（頁面沒有圖片）| error
    error      TEXT,
    updated_at TEXT NOT NULL
);

-- 列表翻頁的進度游標：記住最後處理完的列表頁，下次從那裡接著跑。
-- 一輪跑不完（CI 有時間上限）時靠它續跑；跑到最新一頁後，之後每次執行
-- 都從最新頁開始，只處理新增的內容。
CREATE TABLE IF NOT EXISTS crawl_cursor (
    site          TEXT NOT NULL,
    start_url     TEXT NOT NULL,
    last_page_url TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (site, start_url)
);
"""

# 名稱與說明分散在多筆 image_sources，沒辦法用 external content 直接對映，
# 所以用一般的 FTS5 表，rowid 對齊 images.id，寫入時把該圖的所有名稱／說明串起來。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5 (
    names, descriptions, tokenize={tokenize}
);
"""


@dataclass
class ImageRecord:
    site: str
    page_url: str
    image_url: str
    name: str | None = None
    description: str | None = None
    width: int | None = None
    height: int | None = None
    published_at: str | None = None
    tags: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uniq(values: Iterable[str | None]) -> list[str]:
    """保序去重，順便濾掉空值。"""
    return list(dict.fromkeys(v for v in values if v))


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.has_fts = False
        self._migrate()

    # ---------- schema ----------

    def _migrate(self) -> None:
        # 舊版 storyset 把 images 砍成 (id, image_url, width, height)、image_sources
        # 砍成 (image_id, page_url, name)，site/published_at/fetched_at/description 全沒了。
        # 新 schema 需要這些欄，這裡用 ALTER TABLE 補回：site 填固定值，日期/說明欄留 NULL，
        # 等 crawler 下次重爬對應分頁時靠 upsert 的 COALESCE 補上。資料不會丟。
        self._add_missing_columns("images", {
            "site": "TEXT NOT NULL DEFAULT 'storyset'",
            "published_at": "TEXT",
            "fetched_at": "TEXT NOT NULL DEFAULT ''",
        })
        self._add_missing_columns("image_sources", {
            "description": "TEXT",
            "published_at": "TEXT",
        })
        self.conn.executescript(SCHEMA)
        self.has_fts = self._try_fts()
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        if not self._table_exists(table):
            return  # SCHEMA 會直接建好
        existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _table_exists(self, table: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def _try_fts(self) -> bool:
        # trigram 需要 SQLite >= 3.34；不支援就退回 unicode61
        existed = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='images_fts'"
        ).fetchone()
        for tokenize in ("'trigram'", "'unicode61 remove_diacritics 2'"):
            try:
                self.conn.executescript(FTS_SCHEMA.format(tokenize=tokenize))
                self.has_fts = True  # _index_image 會看這個旗標
                if not existed:
                    self._rebuild_fts()
                return True
            except sqlite3.OperationalError:
                continue
        return False

    def _rebuild_fts(self) -> None:
        """全量重建索引（剛升級或剛建立索引時用）。"""
        self.conn.execute("DELETE FROM images_fts")
        rows = self.conn.execute("SELECT id FROM images").fetchall()
        for row in rows:
            self._index_image(row["id"])

    def _index_image(self, image_id: int) -> None:
        """把這張圖的所有名稱與說明寫進全文索引。"""
        if not self.has_fts:
            return
        row = self.conn.execute(
            """
            SELECT COALESCE(GROUP_CONCAT(name, ' '), '')        AS names,
                   COALESCE(GROUP_CONCAT(description, ' '), '') AS descriptions
            FROM image_sources WHERE image_id = ?
            """,
            (image_id,),
        ).fetchone()
        self.conn.execute("DELETE FROM images_fts WHERE rowid = ?", (image_id,))
        self.conn.execute(
            "INSERT INTO images_fts (rowid, names, descriptions) VALUES (?, ?, ?)",
            (image_id, row["names"], row["descriptions"]),
        )

    # ---------- 寫入 ----------

    def upsert_image(self, rec: ImageRecord) -> int:
        """以 image_url 為鍵寫入或更新，並掛上這一頁的名稱／說明與標籤。"""
        cur = self.conn.execute(
            """
            INSERT INTO images
                (site, image_url, width, height, published_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (image_url) DO UPDATE SET
                site         = excluded.site,
                -- 這次沒量到尺寸就保留舊值，不要用 NULL 蓋掉
                width        = COALESCE(excluded.width,  images.width),
                height       = COALESCE(excluded.height, images.height),
                -- 多個來源頁時取最早的公開日期
                published_at = MIN(
                    COALESCE(excluded.published_at, images.published_at),
                    COALESCE(images.published_at, excluded.published_at)
                ),
                fetched_at   = excluded.fetched_at
            RETURNING id
            """,
            (rec.site, rec.image_url, rec.width, rec.height, rec.published_at, _now()),
        )
        image_id = int(cur.fetchone()[0])

        # 同一張圖在不同來源頁有不同名稱／說明，各自留一筆
        self.conn.execute(
            """
            INSERT INTO image_sources (image_id, page_url, name, description, published_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (image_id, page_url) DO UPDATE SET
                name         = COALESCE(excluded.name, image_sources.name),
                description  = COALESCE(excluded.description, image_sources.description),
                published_at = COALESCE(excluded.published_at, image_sources.published_at)
            """,
            (image_id, rec.page_url, rec.name, rec.description, rec.published_at),
        )

        self._add_tags(image_id, rec.tags)
        self._index_image(image_id)
        return image_id

    def _add_tags(self, image_id: int, tags: Iterable[str]) -> None:
        """標籤取聯集，只增不減。

        一張圖有多個來源頁，各頁標籤不同，這裡是逐頁寫入的，若「以最新一次為準」
        就會把其他來源頁的標籤刪掉。抽取失敗時也一樣不該清空既有資料。
        """
        clean = sorted({t.strip() for t in tags if t and t.strip()})
        if not clean:
            return

        self.conn.executemany(
            "INSERT OR IGNORE INTO tags (name) VALUES (?)", [(t,) for t in clean]
        )
        rows = self.conn.execute(
            f"SELECT id FROM tags WHERE name IN ({','.join('?' * len(clean))})", clean
        ).fetchall()
        self.conn.executemany(
            "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)",
            [(image_id, r["id"]) for r in rows],
        )

    def mark(self, url: str, site: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_state (url, site, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (url) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (url, site, status, error, _now()),
        )

    def is_done(self, url: str) -> bool:
        """done 與 empty 都算處理過；只有 error 會在下次重跑時重試。"""
        row = self.conn.execute(
            "SELECT 1 FROM crawl_state WHERE url = ? AND status IN ('done', 'empty')", (url,)
        ).fetchone()
        return row is not None

    def get_cursor(self, site: str, start_url: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_page_url FROM crawl_cursor WHERE site = ? AND start_url = ?",
            (site, start_url),
        ).fetchone()
        return row["last_page_url"] if row else None

    def set_cursor(self, site: str, start_url: str, last_page_url: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_cursor (site, start_url, last_page_url, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (site, start_url) DO UPDATE SET
                last_page_url = excluded.last_page_url,
                updated_at = excluded.updated_at
            """,
            (site, start_url, last_page_url, _now()),
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        # 把 WAL 併回主檔：CI 只會提交 images.db，殘留在 -wal 的資料等於遺失
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as exc:  # 有其他連線時可能失敗
            import logging

            logging.getLogger(__name__).warning("WAL checkpoint 失敗：%s", exc)
        self.conn.close()

    # ---------- 查詢 ----------

    def search(
        self,
        tags: Sequence[str] = (),
        match_any: bool = False,
        query: str | None = None,
        raw_query: bool = False,
        site: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        sql = "SELECT i.* FROM images i"

        if tags:
            placeholders = ",".join("?" * len(tags))
            sql += (
                " JOIN image_tags it ON it.image_id = i.id"
                " JOIN tags t ON t.id = it.tag_id"
            )
            where.append(f"t.name IN ({placeholders})")
            params.extend(tags)

        if query:
            # trigram 分詞查不到少於 3 個字元的詞，這種短查詢直接走 LIKE
            use_fts = self.has_fts and (raw_query or len(query.strip()) >= 3)
            if use_fts:
                sql += " JOIN images_fts f ON f.rowid = i.id"
                where.append("images_fts MATCH ?")
                # 非 raw 模式把整串包成 phrase，避免 - " * 等字元被當成 FTS 語法
                params.append(query if raw_query else '"' + query.replace('"', '""') + '"')
            else:
                # 名稱與說明都在 image_sources，短查詢用 EXISTS 掃來源表
                where.append(
                    "EXISTS (SELECT 1 FROM image_sources s WHERE s.image_id = i.id"
                    " AND (s.name LIKE ? OR s.description LIKE ?))"
                )
                params.extend([f"%{query}%"] * 2)

        if site:
            where.append("i.site = ?")
            params.append(site)
        if since:
            where.append("i.published_at >= ?")
            params.append(since)
        if until:
            where.append("i.published_at <= ?")
            params.append(until)

        if where:
            sql += " WHERE " + " AND ".join(where)

        if tags and not match_any:
            # AND 語意：命中的相異標籤數必須等於指定數量
            sql += " GROUP BY i.id HAVING COUNT(DISTINCT t.name) = ?"
            params.append(len(tags))
        elif tags:
            sql += " GROUP BY i.id"

        sql += " ORDER BY i.published_at DESC, i.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.conn.execute(sql, params).fetchall()
        return [self._hydrate(dict(row)) for row in rows]

    def _hydrate(self, item: dict[str, Any]) -> dict[str, Any]:
        """補上多值欄位。

        names / descriptions / page_urls 都是清單；同時提供 name / description /
        page_url 這幾個單值欄位（取第一筆）方便只要顯示一個的地方直接用。
        """
        sources = self.sources_of(item["id"])
        item["sources"] = sources
        item["names"] = _uniq(s["name"] for s in sources)
        item["descriptions"] = _uniq(s["description"] for s in sources)
        item["page_urls"] = [s["page_url"] for s in sources]
        item["name"] = item["names"][0] if item["names"] else None
        item["description"] = item["descriptions"][0] if item["descriptions"] else None
        item["page_url"] = item["page_urls"][0] if item["page_urls"] else None
        item["tags"] = self.tags_of(item["id"])
        return item

    def sources_of(self, image_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT page_url, name, description, published_at
            FROM image_sources WHERE image_id = ?
            ORDER BY published_at, page_url
            """,
            (image_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def tags_of(self, image_id: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT t.name FROM tags t
            JOIN image_tags it ON it.tag_id = t.id
            WHERE it.image_id = ?
            ORDER BY t.name
            """,
            (image_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def pages_by_status(self, status: str, site: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT url, error, updated_at FROM crawl_state WHERE status = ?"
        params: list[Any] = [status]
        if site:
            sql += " AND site = ?"
            params.append(site)
        sql += " ORDER BY updated_at DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def images_without_size(self, site: str | None = None, limit: int = 0) -> list[dict[str, Any]]:
        """尺寸沒量到的圖（例如量測當下被 429 擋掉）。"""
        # 量測要帶 Referer，所以順便撈一個來源頁
        sql = """
            SELECT i.id, i.image_url,
                   (SELECT s.page_url FROM image_sources s
                    WHERE s.image_id = i.id LIMIT 1) AS page_url
            FROM images i
            WHERE i.width IS NULL OR i.height IS NULL
        """
        params: list[Any] = []
        if site:
            sql += " AND i.site = ?"
            params.append(site)
        sql += " ORDER BY i.id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def update_size(self, image_id: int, width: int, height: int) -> None:
        self.conn.execute(
            "UPDATE images SET width = ?, height = ? WHERE id = ?", (width, height, image_id)
        )

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "images": one("SELECT COUNT(*) FROM images"),
            "sources": one("SELECT COUNT(*) FROM image_sources"),
            "multi_source": one(
                "SELECT COUNT(*) FROM (SELECT image_id FROM image_sources"
                " GROUP BY image_id HAVING COUNT(*) > 1)"
            ),
            "tags": one("SELECT COUNT(*) FROM tags"),
            "with_size": one("SELECT COUNT(*) FROM images WHERE width IS NOT NULL"),
            "pages_done": one("SELECT COUNT(*) FROM crawl_state WHERE status = 'done'"),
            "pages_empty": one("SELECT COUNT(*) FROM crawl_state WHERE status = 'empty'"),
            "pages_error": one("SELECT COUNT(*) FROM crawl_state WHERE status = 'error'"),
            "fts": self.has_fts,
        }

    def top_tags(self, limit: int = 30) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            """
            SELECT t.name, COUNT(*) AS n FROM tags t
            JOIN image_tags it ON it.tag_id = t.id
            GROUP BY t.id ORDER BY n DESC, t.name LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(r["name"], r["n"]) for r in rows]