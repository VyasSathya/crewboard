# -*- coding: utf-8 -*-
"""The task list file and the one rule every writer follows.

The list is a single JSON file with an integer ``rev``. A write is accepted only when its ``rev`` is
exactly one more than the ``rev`` on disk, so a writer working from a stale copy (the page, an agent,
you in an editor) is refused instead of silently erasing a newer edit. Every accepted version is
copied to ``history/`` next to the file, named by its rev, so nothing written is ever lost.
"""
import json, os, shutil, threading, time

EMPTY = {"rev": 0, "items": [], "focus": None}


class Stale(Exception):
    """The write was made from an older copy. ``current`` is the rev on disk."""
    def __init__(self, current):
        super().__init__("stale: rev on disk is %s" % current)
        self.current = current


class _FileLock:
    """Cross-process lock: a sibling ``.lock`` file created with O_EXCL. Stale locks (older than
    ``stale_after`` seconds, from a crashed writer) are broken."""
    def __init__(self, path, timeout=5.0, stale_after=30.0):
        self.path, self.timeout, self.stale_after = path + ".lock", timeout, stale_after

    def __enter__(self):
        t0 = time.time()
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode()); os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale_after:
                        os.remove(self.path); continue
                except OSError:
                    pass
                if time.time() - t0 > self.timeout:
                    raise TimeoutError("could not lock " + self.path)
                time.sleep(0.02)

    def __exit__(self, *exc):
        try: os.remove(self.path)
        except OSError: pass


class Store:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.history = os.path.join(os.path.dirname(self.path), "history")
        self.stem = os.path.splitext(os.path.basename(self.path))[0]
        self._lock = threading.Lock()

    # ------------------------------------------------------------ reading
    def ensure(self, seed=None):
        """Creates the file (from ``seed`` or an empty list) if it does not exist yet."""
        if os.path.exists(self.path): return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        doc = dict(EMPTY)
        if seed:
            with open(seed, encoding="utf-8") as fh: doc = json.load(fh)
        self._write(doc)

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as fh: return json.load(fh)
        except FileNotFoundError:
            return dict(EMPTY)

    def rev(self):
        return int(self.read().get("rev", 0) or 0)

    # ------------------------------------------------------------ the write rule
    def write(self, doc):
        """Writes ``doc`` if ``doc['rev'] == rev on disk + 1``; raises :class:`Stale` otherwise.
        Returns the new rev."""
        if not isinstance(doc, dict) or not isinstance(doc.get("items", []), list):
            raise ValueError("document must be an object with an items list")
        try: new = int(doc.get("rev"))
        except (TypeError, ValueError): raise ValueError("rev must be an integer")
        with self._lock, _FileLock(self.path):
            cur = self.rev()
            if new != cur + 1:
                raise Stale(cur)
            self._write(doc)
        return new

    def update(self, fn, tries=3):
        """Read, change with ``fn(doc)``, bump rev by exactly 1, write. Retries on a stale write."""
        for _ in range(tries):
            doc = self.read()
            fn(doc)
            doc["rev"] = int(doc.get("rev", 0) or 0) + 1
            doc["updated"] = time.strftime("%Y-%m-%dT%H:%M")
            try:
                return self.write(doc)
            except Stale:
                continue
        raise Stale(self.rev())

    def _write(self, doc):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=True, indent=1)
        if os.path.exists(self.path):
            shutil.copyfile(self.path, self.path + ".bak")
        os.replace(tmp, self.path)
        self.snapshot()

    # ------------------------------------------------------------ history
    def snapshot(self):
        """Copies the current file to history/<stem>-revNNNNN.json if that rev is not kept yet.
        Called after every write here, and by the server's watcher for writes made by anyone else."""
        try:
            rev = self.rev()
            os.makedirs(self.history, exist_ok=True)
            dst = os.path.join(self.history, "%s-rev%05d.json" % (self.stem, rev))
            if not os.path.exists(dst):
                shutil.copyfile(self.path, dst)
            return dst
        except (OSError, ValueError):
            return None

    def watch(self, interval=1.0):
        """Background thread: snapshot whenever the file changes, whoever wrote it."""
        def loop():
            last = None
            while True:
                try:
                    m = os.path.getmtime(self.path)
                    if m != last:
                        last = m; self.snapshot()
                except OSError:
                    pass
                time.sleep(interval)
        t = threading.Thread(target=loop, daemon=True, name="crewboard-history")
        t.start()
        return t


def find(items, item_id):
    """Depth-first search for an item (tasks hold subtasks in ``items``)."""
    for it in items or []:
        if it.get("id") == item_id: return it
        k = find(it.get("items"), item_id)
        if k: return k
    return None
