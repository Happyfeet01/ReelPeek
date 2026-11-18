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


class EmbedFallbackTests(unittest.TestCase):
    SAMPLE_EMBED = (
        '<html><head>'
        '<meta property="og:title" content="Cooles Reel" />'
        '<meta property="og:description" content="Beschreibung mit #Hashtag" />'
        '<meta property="og:video:secure_url" content="https://scontent.cdninstagram.com/v/t66.mp4?efg=abc&amp;oe=1" />'
        '<script>window.__data = {"playable_url":"https:\\/\\/scontent.fra3-1.cdninstagram.com\\/v\\/clip.mp4?token=1",'
        '"username":"reelpeek_fan","display_url":"https:\\/\\/scontent.fra3-1.cdninstagram.com\\/v\\/img.jpg"};</script>'
        '</head></html>'
    )

    def test_variant_url_swaps_path_kind(self):
        from app.extractor import _variant_url
        self.assertEqual(
            _variant_url("https://www.instagram.com/reel/ABC123/"),
            "https://www.instagram.com/p/ABC123/",
        )
        self.assertEqual(
            _variant_url("https://www.instagram.com/p/ABC123/"),
            "https://www.instagram.com/reel/ABC123/",
        )

    def test_variant_url_rejects_non_post_links(self):
        from app.extractor import _variant_url
        self.assertIsNone(_variant_url("https://www.instagram.com/someuser/"))
        self.assertIsNone(_variant_url("https://www.instagram.com/reel/../etc/"))

    def test_cdn_check_only_accepts_instagram_https_hosts(self):
        from app.extractor import _is_instagram_cdn
        self.assertTrue(_is_instagram_cdn("https://scontent.cdninstagram.com/v/x.mp4"))
        self.assertTrue(_is_instagram_cdn("https://scontent.fra3-1.fbcdn.net/v/x.mp4"))
        self.assertTrue(_is_instagram_cdn("https://www.instagram.com/v/x.mp4"))
        self.assertFalse(_is_instagram_cdn("https://evil.example.com/x.mp4"))
        self.assertFalse(_is_instagram_cdn("http://scontent.cdninstagram.com/v/x.mp4"))
        self.assertFalse(_is_instagram_cdn("https://cdninstagram.com.evil.example.com/x.mp4"))

    def test_parse_embed_prefers_json_video_and_reads_metadata(self):
        from app.extractor import _parse_embed
        metadata, media_url, kind = _parse_embed(
            self.SAMPLE_EMBED, "ABC123", "https://www.instagram.com/reel/ABC123/")
        self.assertEqual(kind, "video")
        self.assertEqual(media_url, "https://scontent.fra3-1.cdninstagram.com/v/clip.mp4?token=1")
        self.assertEqual(metadata["id"], "ABC123")
        self.assertEqual(metadata["title"], "Cooles Reel")
        self.assertEqual(metadata["description"], "Beschreibung mit #Hashtag")
        self.assertEqual(metadata["uploader"], "reelpeek_fan")

    def test_parse_embed_falls_back_to_og_video_and_unescapes_amp(self):
        from app.extractor import _parse_embed
        html = '<meta property="og:video" content="https://video.fbcdn.net/v/x.mp4?efg=1&amp;oe=2" />'
        _metadata, media_url, kind = _parse_embed(html, "ABC123", "u")
        self.assertEqual(kind, "video")
        self.assertEqual(media_url, "https://video.fbcdn.net/v/x.mp4?efg=1&oe=2")

    def test_parse_embed_image_fallback(self):
        from app.extractor import _parse_embed
        html = '<meta property="og:image" content="https://scontent.cdninstagram.com/v/pic.jpg" />'
        _metadata, media_url, kind = _parse_embed(html, "ABC123", "u")
        self.assertEqual((kind, media_url), ("image", "https://scontent.cdninstagram.com/v/pic.jpg"))

    def test_parse_embed_ignores_foreign_media_hosts(self):
        from app.extractor import _parse_embed
        html = ('<meta property="og:video" content="https://evil.example.com/x.mp4" />'
                '<script>{"playable_url":"https://evil.example.com/y.mp4","username":"x"}</script>')
        _metadata, media_url, kind = _parse_embed(html, "ABC123", "u")
        self.assertEqual((media_url, kind), ("", ""))


class EmbedHttpTests(unittest.TestCase):
    def _serve(self, handler):
        import threading
        from http.server import HTTPServer
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_http_download_detects_type_and_writes_file(self):
        from http.server import BaseHTTPRequestHandler
        from app.extractor import _http_download
        payload = b"\x00\x01\x02" * 512

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = self._serve(Handler)
        with tempfile.TemporaryDirectory() as directory:
            target = _http_download(
                f"http://127.0.0.1:{server.server_port}/media/clip", Path(directory) / "001_ABC123")
            self.assertEqual(target.suffix, ".mp4")
            self.assertEqual(target.read_bytes(), payload)

    def test_run_embed_downloads_video_from_embed_page(self):
        from http.server import BaseHTTPRequestHandler
        from unittest import mock
        from app.extractor import _run_embed
        video = b"FAKEMP4" * 100

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("/embed/captioned/"):
                    body = (
                        '<meta property="og:description" content="Capt" />'
                        f'<script>{{"playable_url":"http://127.0.0.1:{server.server_port}/v/clip.mp4",'
                        '"username":"tester"}</script>'
                    ).encode()
                    content_type = "text/html"
                elif self.path == "/v/clip.mp4":
                    body, content_type = video, "video/mp4"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = self._serve(Handler)
        base = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("app.extractor.EMBED_HOST", base), \
                    mock.patch("app.extractor._is_instagram_cdn", lambda value: True):
                metadata = _run_embed("https://www.instagram.com/reel/ABC123/", Path(directory))
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "001_ABC123.mp4")
            self.assertEqual(files[0].read_bytes(), video)
            self.assertEqual(metadata["uploader"], "tester")
            self.assertEqual(metadata["description"], "Capt")

    def test_run_embed_reports_login_redirect(self):
        from http.server import BaseHTTPRequestHandler
        from unittest import mock
        from app.extractor import ExtractionError, _run_embed

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{server.server_port}/accounts/login/?next=x")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = self._serve(Handler)
        base = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("app.extractor.EMBED_HOST", base):
                with self.assertRaises(ExtractionError) as context:
                    _run_embed("https://www.instagram.com/reel/ABC123/", Path(directory))
            self.assertIn("Anmeldung", str(context.exception))
            self.assertEqual(list(Path(directory).iterdir()), [])


class CommentTimestampTests(unittest.TestCase):
    def test_numeric_comment_timestamp_is_formatted(self):
        from app.extractor import _comments
        comments = _comments({"comments": [
            {"text": "Hallo", "owner": {"username": "u"}, "created_at": 1_700_000_000},
        ]})
        self.assertEqual(comments[0]["created_at"], "2023-11-14T22:13:20+00:00")


if __name__ == "__main__":
    unittest.main()
