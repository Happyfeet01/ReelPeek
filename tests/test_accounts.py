import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import accounts, main
from app.database import Database


class AccountFlowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.db = Database(self.root / "reelpeek.sqlite3")
        self.enterContext(patch.multiple(main, database=self.db, ADMIN_EMAIL="owner@example.org",
                                         DATA_ROOT=self.root, SESSION_SECRET="z"*40))
        self.send = self.enterContext(patch.object(accounts, "send_link"))
        self.admin = self.enterContext(TestClient(main.app, base_url="https://testserver"))
        self.member = TestClient(main.app, base_url="https://testserver")
        self.addCleanup(self.member.close)
        self.password = "a sufficiently long passphrase"

    def post(self, client, path, form_page, **data):
        html = client.get(form_page).text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
        return client.post(path, data=dict(csrf_token=csrf, **data), follow_redirects=False)

    def activate(self, client, email):
        token = self.send.call_args.args[1]
        self.assertEqual(self.post(client, "/login", "/login", email=email,
                                   password=self.password).status_code, 401)
        self.assertEqual(self.post(client, "/aktivieren", "/aktivieren", token=token,
                                   password=self.password).status_code, 200)
        self.assertEqual(self.post(client, "/aktivieren", "/aktivieren", token=token,
                                   password=self.password).status_code, 400)
        self.assertEqual(self.post(client, "/login", "/login", email=email,
                                   password=self.password).status_code, 303)

    def test_invitations_isolation_and_account_management(self):
        self.post(self.admin, "/registrieren", "/registrieren", email="unknown@example.org")
        self.send.assert_not_called()
        self.post(self.admin, "/registrieren", "/registrieren", email=main.ADMIN_EMAIL)
        self.activate(self.admin, main.ADMIN_EMAIL)
        email = "member@example.org"
        self.assertEqual(self.post(self.admin, "/admin/invite", "/admin", email=email).status_code, 303)
        self.activate(self.member, email)
        self.assertEqual(self.member.get("/admin").status_code, 403)
        url = "https://www.instagram.com/reel/TEST/"
        owner_item = self.db.create(url, 1)
        member_item = self.db.create(url, 2)
        self.assertNotEqual(owner_item["id"], member_item["id"])
        item_id = member_item["id"]
        self.assertEqual(self.admin.get(f"/items/{item_id}").status_code, 404)
        self.assertEqual(self.post(self.admin, f"/items/{item_id}/delete", "/").status_code, 404)
        file = self.root / "media" / item_id / "01.mp4"
        file.parent.mkdir()
        file.write_bytes(b"video")
        path = f"/media/{item_id}/01.mp4"
        thumb = self.root / "thumbnails" / f"{item_id}.jpg"
        thumb.write_bytes(b"thumbnail")
        thumb_url = f"/thumbnails/{item_id}.jpg"
        self.db.set_download(item_id, title="Member", description="", uploader="",
                             thumbnail_path=thumb_url, media=[{"url": path}], media_kind="video")
        for location in (path, thumb_url):
            self.assertEqual(self.member.get(location).status_code, 200)
            self.assertEqual(self.admin.get(location).status_code, 404)
        self.post(self.admin, "/admin/2/sperren", "/admin")
        self.post(self.admin, "/admin/2/freigeben", "/admin")
        self.assertEqual(self.member.get(path, follow_redirects=False).status_code, 303)
        self.post(self.admin, "/admin/2/loeschen", "/admin", confirm=email)
        self.assertFalse(file.exists())
        self.assertFalse(thumb.exists())
        self.assertIsNone(self.db.get(item_id, 2))
        self.assertIsNotNone(self.db.get(owner_item["id"], 1))

    def test_public_pages_and_expired_link(self):
        for path in ("/hilfe", "/datenschutz", "/impressum"):
            self.assertEqual(self.member.get(path, follow_redirects=False).status_code, 200)
        with patch.object(main, "RETENTION_DAYS", 9):
            self.assertIn("9 Tagen", self.member.get("/hilfe").text)
        self.post(self.admin, "/registrieren", "/registrieren", email=main.ADMIN_EMAIL)
        with self.db.connect() as conn:
            conn.execute("UPDATE accounts SET token_until=0")
        self.assertEqual(self.post(self.admin, "/aktivieren", "/aktivieren",
                                   token=self.send.call_args.args[1], password=self.password).status_code, 400)
