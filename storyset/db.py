from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id           INTEGER PRIMARY KEY,
    site         TEXT    NOT NULL,
    image_url    TEXT    NOT NULL UNIQUE,
    width        INTEGER,
    height       INTEGER,
    published_at TEXT,
    fetched_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_site ON images (site);
CREATE INDEX IF NOT EXISTS idx_images_published ON images (published_at);

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
    tag_id   INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (image_id, tag_id)
) WITHOUT ROWID;
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


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = OFF")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()
        self.conn.commit()

    def _ensure_schema(self) -> None:
        if not self._table_exists("images"):
            self.conn.executescript(SCHEMA)
            return

        image_cols = self._table_columns("images")
        if "published_at" not in image_cols:
            self.conn.execute("ALTER TABLE images ADD COLUMN published_at TEXT")
        if "fetched_at" not in image_cols:
            self.conn.execute("ALTER TABLE images ADD COLUMN fetched_at TEXT NOT NULL DEFAULT ''")
        if "site" in image_cols:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_images_site ON images (site)")
        if "published_at" in self._table_columns("images"):
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_images_published ON images (published_at)")

        if not self._table_exists("image_sources"):
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS image_sources (
                    image_id     INTEGER NOT NULL REFERENCES images (id) ON DELETE CASCADE,
                    page_url     TEXT    NOT NULL,
                    name         TEXT,
                    description  TEXT,
                    published_at TEXT,
                    PRIMARY KEY (image_id, page_url)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS idx_image_sources_name ON image_sources (name);
                """
            )
        else:
            source_cols = self._table_columns("image_sources")
            if "name" not in source_cols:
                self.conn.execute("ALTER TABLE image_sources ADD COLUMN name TEXT")
            if "description" not in source_cols:
                self.conn.execute("ALTER TABLE image_sources ADD COLUMN description TEXT")
            if "published_at" not in source_cols:
                self.conn.execute("ALTER TABLE image_sources ADD COLUMN published_at TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_image_sources_name ON image_sources (name)")

        if not self._table_exists("tags"):
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tags (
                    id   INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE
                );
                """
            )

        if not self._table_exists("image_tags"):
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS image_tags (
                    image_id INTEGER NOT NULL REFERENCES images (id) ON DELETE CASCADE,
                    tag_id   INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
                    PRIMARY KEY (image_id, tag_id)
                ) WITHOUT ROWID;
                """
            )

    def _table_exists(self, table_name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _table_columns(self, table_name: str) -> list[str]:
        return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()]

    def upsert_image(self, rec: ImageRecord) -> int:
        cur = self.conn.execute(
            "SELECT id, width, height, published_at FROM images WHERE image_url = ?",
            (rec.image_url,),
        )
        row = cur.fetchone()

        if row:
            width = rec.width if rec.width is not None else row["width"]
            height = rec.height if rec.height is not None else row["height"]
            published_at = self._earlier_date(rec.published_at, row["published_at"])
            self.conn.execute(
                """
                UPDATE images
                   SET site = ?, width = ?, height = ?, published_at = ?, fetched_at = ?
                 WHERE id = ?
                """,
                (rec.site, width, height, published_at, _now(), row["id"]),
            )
            image_id = int(row["id"])
        else:
            self.conn.execute(
                """
                INSERT INTO images (site, image_url, width, height, published_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (rec.site, rec.image_url, rec.width, rec.height, rec.published_at, _now()),
            )
            image_id = int(self.conn.execute(
                "SELECT id FROM images WHERE image_url = ?",
                (rec.image_url,),
            ).fetchone()[0])

        self.conn.execute(
            """
            INSERT INTO image_sources (image_id, page_url, name, description, published_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (image_id, page_url) DO UPDATE SET
                name = COALESCE(excluded.name, image_sources.name),
                description = COALESCE(excluded.description, image_sources.description),
                published_at = COALESCE(excluded.published_at, image_sources.published_at)
            """,
            (image_id, rec.page_url, rec.name, rec.description, rec.published_at),
        )

        self._add_tags(image_id, rec.tags)
        return image_id

    def _add_tags(self, image_id: int, tags: Iterable[str]) -> None:
        cleaned = [tag.strip() for tag in tags if tag and tag.strip()]
        if not cleaned:
            return

        self.conn.executemany(
            "INSERT OR IGNORE INTO tags (name) VALUES (?)",
            [(tag,) for tag in cleaned],
        )
        rows = self.conn.execute(
            f"SELECT id FROM tags WHERE name IN ({','.join('?' for _ in cleaned)})",
            cleaned,
        ).fetchall()
        self.conn.executemany(
            "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)",
            [(image_id, row["id"]) for row in rows],
        )

    def _earlier_date(self, first: str | None, second: str | None) -> str | None:
        if first and second:
            return first if first < second else second
        return first or second

    def commit(self) -> None:
        self.conn.commit()

    def existing_image_urls(self) -> set[str]:
        rows = self.conn.execute("SELECT image_url FROM images").fetchall()
        return {row[0] for row in rows if row[0]}

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ---------- read helpers for export/viewer ----------
    def sources_of(self, image_id: int) -> list[dict[str, str | None]]:
        rows = self.conn.execute(
            "SELECT page_url, name FROM image_sources WHERE image_id = ? ORDER BY page_url",
            (image_id,),
        ).fetchall()
        # return minimal fields (no description/published_at)
        return [{"page_url": r[0], "name": r[1]} for r in rows]

    def tags_of(self, image_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT t.name FROM tags t JOIN image_tags it ON it.tag_id = t.id WHERE it.image_id = ? ORDER BY t.name",
            (image_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def export_all(self) -> list[dict[str, object]]:
        """Return a list of image records suitable for writing to images.json/viewer.

        Each item will include image_url, width, height, published_at, sources (list), tags (list).
        """
        rows = self.conn.execute("SELECT id, image_url, width, height FROM images ORDER BY id DESC").fetchall()
        out: list[dict[str, object]] = []
        for row in rows:
            img_id = row[0]
            item = {
                "image_url": row[1],
                "width": row[2],
                "height": row[3],
                "sources": self.sources_of(img_id),
                "tags": self.tags_of(img_id),
            }
            out.append(item)
        return out

    def migrate_remove_unused_columns(self) -> None:
        """Migrate the database to remove unused columns while preserving data.

        This will:
        - Replace `images` with a table containing only (id, image_url, width, height).
        - Replace `image_sources` with a table containing only (image_id, page_url, name).
        - Preserve `tags` and `image_tags`.

        Safe to call multiple times; will no-op if columns already absent.
        """
        # Check current columns
        img_cols = set(self._table_columns("images"))
        src_cols = set(self._table_columns("image_sources"))

        need_images_migration = bool({"site", "published_at", "fetched_at"} & img_cols)
        need_sources_migration = "description" in src_cols or "published_at" in src_cols

        if not need_images_migration and not need_sources_migration:
            return

        cur = self.conn.cursor()
        try:
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute("BEGIN")

            if need_images_migration:
                # create new images table without the extra columns
                cur.execute(
                    "CREATE TABLE new_images (id INTEGER PRIMARY KEY, image_url TEXT NOT NULL UNIQUE, width INTEGER, height INTEGER)"
                )
                cur.execute(
                    "INSERT INTO new_images (id, image_url, width, height) SELECT id, image_url, width, height FROM images"
                )
                cur.execute("DROP TABLE images")
                cur.execute("ALTER TABLE new_images RENAME TO images")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_images_published ON images (id)")

            if need_sources_migration:
                # create new image_sources without description/published_at
                cur.execute(
                    "CREATE TABLE new_image_sources (image_id INTEGER NOT NULL REFERENCES images (id) ON DELETE CASCADE, page_url TEXT NOT NULL, name TEXT, PRIMARY KEY (image_id, page_url)) WITHOUT ROWID"
                )
                # copy over existing page_url and name
                cur.execute(
                    "INSERT OR IGNORE INTO new_image_sources (image_id, page_url, name) SELECT image_id, page_url, name FROM image_sources"
                )
                cur.execute("DROP TABLE image_sources")
                cur.execute("ALTER TABLE new_image_sources RENAME TO image_sources")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_image_sources_name ON image_sources (name)")

            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
