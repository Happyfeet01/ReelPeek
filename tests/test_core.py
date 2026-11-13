import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.extractor import canonicalize_instagram_url


class UrlTests(unittest.TestCase):
    def test_canonicalizes_reel(self):
        self.assertEqual(
            canonicalize_instagram_url("https://www.instagram.com/reel/ABC123/?utm_source=x"),
            "https://www.instagram.com/reel/ABC123/",
        )

    def test_rejects_other_hosts(self):
        with self.assertRaises(ValueError):
            canonicalize_instagram_url("https://example.com/reel/ABC123")


class DatabaseTests(unittest.TestCase):
    def test_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "test.sqlite3")
            database.initialize()
            item = database.create("https://www.instagram.com/reel/ABC123/")
            self.assertFalse(item["seen"])
            database.mark_seen(item["id"])
            self.assertTrue(database.get(item["id"])["seen"])
            database.toggle_favorite(item["id"])
            self.assertTrue(database.get(item["id"])["favorite"])

    def test_error_clears_stale_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "test.sqlite3")
            database.initialize()
            item = database.create("https://www.instagram.com/p/XYZ789/")
            database.set_download(
                item["id"],
                title="Test",
                description="",
                uploader="",
                thumbnail_path=None,
                media=[{"url": "/media/file.mp4", "kind": "video", "mime": "video/mp4"}],
                media_kind="video",
            )
            database.set_error(item["id"], "Abruf fehlgeschlagen")
            failed = database.get(item["id"])
            self.assertEqual(failed["media"], [])
            self.assertEqual(failed["download_state"], "error")


class CaptionTests(unittest.TestCase):
    def test_reads_gallery_caption(self):
        from app.extractor import _description
        sample = {"caption": {"text": "Ein schöner Tag! 🌅\n#Natur"}}
        self.assertEqual(_description(sample), "Ein schöner Tag! 🌅\n#Natur")

    def test_reads_instagram_graphql_caption(self):
        from app.extractor import _description
        sample = {"graphql": {"shortcode_media": {
            "edge_media_to_caption": {"edges": [{"node": {"text": "Hallo #Reels"}}]}
        }}}
        self.assertEqual(_description(sample), "Hallo #Reels")

    def test_prefers_sidecar_with_caption(self):
        import json
        from app.extractor import _read_metadata
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "1.json").write_text(json.dumps({"id": "xyz"}))
            (folder / "2.json").write_text(json.dumps({"description": "Beschreibung"}))
            self.assertEqual(_read_metadata(folder)["description"], "Beschreibung")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "reelpeek.sqlite3")
        self.db.initialize()

    def create_download(self, name: str, *, favorite=False, days_ago=15):
        from datetime import datetime, timedelta, timezone
        item = self.db.create(f"https://www.instagram.com/reel/{name}/")
        media_path = self.root / "media" / item["id"]
        media_path.mkdir(parents=True, exist_ok=True)
        (media_path / "01.mp4").write_bytes(b"video")
        thumb = self.root / "thumbnails"
        thumb.mkdir(parents=True, exist_ok=True)
        (thumb / f'{item["id"]}.jpg').write_bytes(b"thumb")
        self.db.set_download(
            item["id"], title="Video", description="Beschreibung", uploader="tester",
            thumbnail_path=f'/thumbnails/{item["id"]}.jpg',
            media=[{"url": f'/media/{item["id"]}/01.mp4', "kind": "video", "mime": "video/mp4"}],
            media_kind="video",
        )
        if favorite:
            self.db.toggle_favorite(item["id"])
        with self.db.connect() as connection:
            timestamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
            connection.execute("UPDATE items SET downloaded_at = ? WHERE id = ?", (timestamp, item["id"]))
        return item["id"]

    def test_only_old_unfavorited_media_are_deleted(self):
        from app.cleanup import prune_media
        old = self.create_download("OLD", days_ago=15)
        favorite = self.create_download("FAV", favorite=True, days_ago=50)
        recent = self.create_download("RECENT", days_ago=1)
        self.assertEqual(prune_media(self.db, self.root, days=14, dry_run=True), 1)
        self.assertTrue((self.root / "media" / old / "01.mp4").exists())
        self.assertEqual(prune_media(self.db, self.root, days=14), 1)
        self.assertEqual(self.db.get(old)["download_state"], "expired")
        self.assertEqual(self.db.get(old)["description"], "Beschreibung")
        self.assertIsNone(self.db.get(old)["thumbnail_path"])
        self.assertFalse((self.root / "media" / old).exists())
        self.assertFalse((self.root / "thumbnails" / f"{old}.jpg").exists())
        self.assertTrue((self.root / "media" / favorite).exists())
        self.assertTrue((self.root / "media" / recent).exists())

    def test_favorite_changed_after_candidate_selection_is_safe(self):
        from datetime import datetime, timedelta, timezone
        item_id = self.create_download("RACE", days_ago=30)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
        self.assertEqual(len(self.db.expired_candidates(cutoff)), 1)
        self.db.toggle_favorite(item_id)
        self.assertFalse(self.db.expire_if_eligible(item_id, cutoff, self.root))
        self.assertTrue((self.root / "media" / item_id / "01.mp4").exists())

    def test_unfavorite_restarts_original_age_not_ttl(self):
        from app.cleanup import prune_media
        item_id = self.create_download("UNFAV", favorite=True, days_ago=40)
        self.assertEqual(prune_media(self.db, self.root, days=14), 0)
        self.db.toggle_favorite(item_id)
        self.assertEqual(prune_media(self.db, self.root, days=14), 1)

    def test_viewing_does_not_extend_retention(self):
        from app.cleanup import prune_media
        item_id = self.create_download("VIEW", days_ago=17)
        self.db.mark_seen(item_id)
        self.assertEqual(prune_media(self.db, self.root, days=14), 1)

    def test_legacy_database_migration_keeps_favorites(self):
        import sqlite3
        # v0.1.0 schema is reused, but downloaded_at is removed to test migration.
        legacy = self.root / "legacy.sqlite3"
        with sqlite3.connect(legacy) as conn:
            conn.execute("""CREATE TABLE items (
              id TEXT PRIMARY KEY, source_url TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL DEFAULT 'Instagram-Beitrag',
              description TEXT NOT NULL DEFAULT '', uploader TEXT NOT NULL DEFAULT '',
              thumbnail_path TEXT, media_json TEXT NOT NULL DEFAULT '[]',
              media_kind TEXT NOT NULL DEFAULT 'video', seen INTEGER NOT NULL DEFAULT 0,
              favorite INTEGER NOT NULL DEFAULT 0, download_state TEXT NOT NULL DEFAULT 'pending',
              error TEXT, created_at TEXT NOT NULL, watched_at TEXT, updated_at TEXT NOT NULL
            )""")
            conn.execute("""INSERT INTO items(id, source_url, favorite, download_state,
                created_at, updated_at) VALUES ('abcdefabcdefabcdefabcdefabcdefab',
                'https://www.instagram.com/reel/LEGACY/',1,'ready',
                '2025-01-01T00:00:00+00:00', '2025-01-20T00:00:00+00:00')""")
        db = Database(legacy)
        db.initialize()
        self.assertTrue(db.get("abcdefabcdefabcdefabcdefabcdefab")["favorite"])
        self.assertEqual(db.get("abcdefabcdefabcdefabcdefabcdefab")["downloaded_at"],
                         "2025-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
