from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT 'Instagram-Beitrag',
                    description TEXT NOT NULL DEFAULT '',
                    uploader TEXT NOT NULL DEFAULT '',
                    thumbnail_path TEXT,
                    media_json TEXT NOT NULL DEFAULT '[]',
                    media_kind TEXT NOT NULL DEFAULT 'video',
                    comments_json TEXT NOT NULL DEFAULT '[]',
                    seen INTEGER NOT NULL DEFAULT 0,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    download_state TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    watched_at TEXT,
                    updated_at TEXT NOT NULL,
                    downloaded_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_items_created_at
                    ON items(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_items_inbox
                    ON items(seen, created_at DESC);
                """
            )
            # Existing v0.1.0 databases used created_at as the first download date.
            # Add a dedicated timestamp without changing existing records or favorites.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(items)")}
            if "downloaded_at" not in columns:
                connection.execute("ALTER TABLE items ADD COLUMN downloaded_at TEXT")
            if "comments_json" not in columns:
                connection.execute(
                    "ALTER TABLE items ADD COLUMN comments_json TEXT NOT NULL DEFAULT '[]'"
                )
            connection.execute("""
                UPDATE items SET downloaded_at = created_at
                WHERE downloaded_at IS NULL AND download_state IN ('ready', 'expired')
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_items_retention
                ON items(favorite, download_state, downloaded_at)
            """)

    @staticmethod
    def _item(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["media"] = json.loads(item.pop("media_json") or "[]")
        item["comments"] = json.loads(item.pop("comments_json", None) or "[]")
        item["seen"] = bool(item["seen"])
        item["favorite"] = bool(item["favorite"])
        return item

    def create(self, source_url: str) -> dict:
        item_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO items (id, source_url, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (item_id, source_url, now, now),
            )
        return self.get(item_id)  # type: ignore[return-value]

    def get(self, item_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        return self._item(row)

    def get_by_url(self, source_url: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE source_url = ?", (source_url,)
            ).fetchone()
        return self._item(row)

    def list(self, view: str = "all", limit: int = 100) -> list[dict]:
        clauses = {
            "inbox": "WHERE seen = 0",
            "favorites": "WHERE favorite = 1",
            "history": "WHERE seen = 1",
            "all": "",
        }
        clause = clauses.get(view, "")
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM items {clause} ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._item(row) for row in rows if row is not None]  # type: ignore[misc]

    def set_download(
        self,
        item_id: str,
        *,
        title: str,
        description: str,
        uploader: str,
        thumbnail_path: str | None,
        media: list[dict],
        media_kind: str,
        comments: list[dict] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE items
                SET title = ?, description = ?, uploader = ?, thumbnail_path = ?,
                    media_json = ?, media_kind = ?, comments_json = ?,
                    download_state = 'ready',
                    error = NULL, updated_at = ?, downloaded_at = ?
                WHERE id = ?
                """,
                (
                    title[:500] or "Instagram-Beitrag",
                    description[:10000],
                    uploader[:250],
                    thumbnail_path,
                    json.dumps(media, ensure_ascii=False),
                    media_kind,
                    json.dumps((comments or [])[:50], ensure_ascii=False),
                    utc_now(),
                    utc_now(),
                    item_id,
                ),
            )

    def set_error(self, item_id: str, message: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE items SET media_json = '[]', download_state = 'error',
                    error = ?, updated_at = ?
                WHERE id = ?
                """,
                (message[:2000], utc_now(), item_id),
            )

    def set_expired(self, item_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE items SET media_json = '[]', thumbnail_path = NULL,
                    download_state = 'expired', updated_at = ? WHERE id = ?
                """,
                (utc_now(), item_id),
            )

    def mark_seen(self, item_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE items SET seen = 1, watched_at = COALESCE(watched_at, ?),
                    updated_at = ? WHERE id = ?
                """,
                (utc_now(), utc_now(), item_id),
            )

    def toggle_favorite(self, item_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE items SET favorite = CASE favorite WHEN 1 THEN 0 ELSE 1 END,
                    updated_at = ? WHERE id = ?
                """,
                (utc_now(), item_id),
            )

    def delete(self, item_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM items WHERE id = ?", (item_id,))

    def expired_candidates(self, cutoff: str) -> list[dict]:
        with self.connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(items)")}
            # Read-only previews also work against v0.1.0 before migration.
            date_column = "downloaded_at" if "downloaded_at" in columns else "created_at"
            rows = connection.execute(
                f"""
                SELECT * FROM items
                WHERE favorite = 0 AND download_state = 'ready'
                    AND {date_column} IS NOT NULL AND {date_column} < ?
                """,
                (cutoff,),
            ).fetchall()
        return [self._item(row) for row in rows if row is not None]  # type: ignore[misc]

    def expire_if_eligible(self, item_id: str, cutoff: str, data_root: Path) -> bool:
        """Recheck the favorite flag under an exclusive write transaction.

        Only paths derived from internal UUIDs are ever removed. The item
        record, URL, caption, author and watched status remain in history.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", item_id):
            return False
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            eligible = connection.execute(
                """SELECT 1 FROM items WHERE id = ? AND favorite = 0
                   AND download_state = 'ready'
                   AND downloaded_at IS NOT NULL AND downloaded_at < ?""",
                (item_id, cutoff),
            ).fetchone()
            if not eligible:
                return False
            media_dir = data_root / "media" / item_id
            if media_dir.is_symlink():
                media_dir.unlink()
            elif media_dir.exists():
                shutil.rmtree(media_dir)
            (data_root / "thumbnails" / f"{item_id}.jpg").unlink(missing_ok=True)
            connection.execute(
                """UPDATE items SET media_json = '[]', thumbnail_path = NULL,
                   download_state = 'expired', updated_at = ? WHERE id = ?""",
                (utc_now(), item_id),
            )
            return True
