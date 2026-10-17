"""The HTTP layer: the page, the feed and the rev-checked save, on a real server bound to a free port."""
import json, os, tempfile, threading, unittest, urllib.error, urllib.request

from crewboard.server import make_server
from crewboard.store import Store


class FakeFeed:
    def snapshot(self): return b'{"now": 0, "items": []}'


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.dir.name, "progress.json")); cls.store.ensure()
        cls.srv = make_server(cls.store, FakeFeed(), port=0)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.dir.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r: return r.status, r.read()

    def post(self, doc):
        req = urllib.request.Request(self.base + "/progress", data=json.dumps(doc).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

    def test_serves_page_feed_and_list(self):
        self.assertIn(b"<title>Hub</title>", self.get("/")[1])
        self.assertEqual(self.get("/app.js")[0], 200)
        self.assertEqual(json.loads(self.get("/hub.json")[1])["items"], [])
        self.assertIn("rev", json.loads(self.get("/progress.json")[1]))

    def test_save_rule_over_http(self):
        rev = self.store.rev()
        self.assertEqual(self.post({"rev": rev + 1, "items": []}), (200, {"ok": True, "rev": rev + 1}))
        code, body = self.post({"rev": rev + 1, "items": [{"id": "x"}]})
        self.assertEqual(code, 409); self.assertEqual(body["rev"], rev + 1)
        code, _ = self.post({"rev": "nope", "items": []})
        self.assertEqual(code, 400)
        self.assertEqual(self.get("/history/progress-rev%05d.json" % (rev + 1))[0], 200)

    def test_no_path_escape(self):
        for p in ("/../progress.json", "/history/../../secret", "/%2e%2e/%2e%2e/etc/passwd"):
            try: code = self.get(p)[0]
            except urllib.error.HTTPError as e: code = e.code
            self.assertEqual(code, 404, p)


if __name__ == "__main__":
    unittest.main()
