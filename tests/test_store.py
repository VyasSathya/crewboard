"""The write rule: a write is accepted only at rev on disk + 1; every accepted version is kept."""
import json, os, tempfile, threading, unittest

from crewboard.store import Stale, Store, find


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "progress.json")
        self.store = Store(self.path)
        self.store.ensure()

    def tearDown(self):
        self.dir.cleanup()

    def doc(self, rev, title="a"):
        return {"rev": rev, "items": [{"id": "t1", "title": title}]}

    def test_new_file_starts_at_rev_zero(self):
        self.assertEqual(self.store.rev(), 0)
        self.assertEqual(self.store.read()["items"], [])

    def test_accepts_exactly_one_more(self):
        self.assertEqual(self.store.write(self.doc(1)), 1)
        self.assertEqual(self.store.write(self.doc(2)), 2)
        self.assertEqual(self.store.rev(), 2)

    def test_refuses_same_rev(self):
        self.store.write(self.doc(1))
        with self.assertRaises(Stale) as cm:
            self.store.write(self.doc(1, "older copy"))
        self.assertEqual(cm.exception.current, 1)
        self.assertEqual(self.store.read()["items"][0]["title"], "a")

    def test_refuses_skipping_ahead(self):
        with self.assertRaises(Stale):
            self.store.write(self.doc(5))
        self.assertEqual(self.store.rev(), 0)

    def test_refuses_going_back(self):
        self.store.write(self.doc(1)); self.store.write(self.doc(2))
        with self.assertRaises(Stale):
            self.store.write(self.doc(1))

    def test_rejects_bad_documents(self):
        for bad in ([], {"rev": "x", "items": []}, {"rev": 1, "items": {}}, {"items": []}):
            with self.assertRaises((ValueError, Stale)):
                self.store.write(bad)

    def test_writes_ascii_only(self):
        self.store.write({"rev": 1, "items": [{"id": "t", "title": "café — done"}]})
        with open(self.path, "rb") as fh: raw = fh.read()
        self.assertTrue(all(b < 128 for b in raw))
        self.assertEqual(self.store.read()["items"][0]["title"], "café — done")

    def test_history_keeps_every_rev(self):
        for r in (1, 2, 3): self.store.write(self.doc(r, "v%d" % r))
        names = sorted(os.listdir(self.store.history))
        self.assertEqual(names, ["progress-rev00000.json", "progress-rev00001.json", "progress-rev00002.json", "progress-rev00003.json"])
        with open(os.path.join(self.store.history, "progress-rev00002.json")) as fh:
            self.assertEqual(json.load(fh)["items"][0]["title"], "v2")

    def test_history_never_overwrites(self):
        self.store.write(self.doc(1, "first"))
        snap = os.path.join(self.store.history, "progress-rev00001.json")
        with open(self.path, "w") as fh: json.dump(self.doc(1, "edited by hand"), fh)   # same rev, someone else's write
        self.store.snapshot()
        with open(snap) as fh: self.assertEqual(json.load(fh)["items"][0]["title"], "first")

    def test_snapshot_picks_up_outside_writes(self):
        with open(self.path, "w") as fh: json.dump(self.doc(7, "agent"), fh)
        self.store.snapshot()
        self.assertTrue(os.path.exists(os.path.join(self.store.history, "progress-rev00007.json")))

    def test_update_bumps_by_one_and_retries(self):
        calls = []

        def fn(doc):
            calls.append(doc["rev"])
            if len(calls) == 1:   # someone else writes between our read and our write
                self.store.write({"rev": doc["rev"] + 1, "items": doc["items"]})
            doc["items"].append({"id": "n%d" % len(calls), "title": "mine"})
        self.assertEqual(self.store.update(fn), 2)
        self.assertEqual(calls, [0, 1])
        self.assertEqual([i["id"] for i in self.store.read()["items"]], ["n2"])

    def test_concurrent_writers_never_both_win(self):
        results = []

        def go():
            try: results.append(self.store.write(self.doc(1)))
            except Stale: results.append("stale")
        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(results.count(1), 1)
        self.assertEqual(results.count("stale"), 7)

    def test_ensure_seeds_from_example(self):
        seed = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crewboard", "examples", "progress.json")
        other = Store(os.path.join(self.dir.name, "sub", "list.json"))
        other.ensure(seed)
        self.assertTrue(find(other.read()["items"], "flaky-checkout"))

    def test_find_searches_subtasks(self):
        items = [{"id": "a", "items": [{"id": "b", "items": [{"id": "c"}]}]}]
        self.assertEqual(find(items, "c"), {"id": "c"})
        self.assertIsNone(find(items, "z"))


if __name__ == "__main__":
    unittest.main()
