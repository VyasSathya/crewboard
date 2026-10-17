# -*- coding: utf-8 -*-
"""The hub's HTTP server. Standard library only.

  GET  /                 the page (web/index.html)
  GET  /hub.json         live sessions and agents, read from transcripts (read-only)
  GET  /progress.json    the task list
  POST /progress         save the task list; refused with 409 unless rev == rev on disk + 1
  GET  /history/<file>   any saved version of the task list

It binds to 127.0.0.1 only: the page shows what your sessions are doing, so it is not for a network.
"""
import http.server, json, mimetypes, os, socketserver, sys, urllib.parse

from .store import Stale

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 64 * 1024 * 1024


class Handler(http.server.BaseHTTPRequestHandler):
    store = None   # set by make_server
    feed = None
    server_version = "crewboard"

    def log_message(self, *a): pass

    # ------------------------------------------------------------ helpers
    def _send(self, code, body, ctype, head=False, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if head: return
        try: self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError): pass

    def _json(self, code, obj, head=False):
        self._send(code, json.dumps(obj, ensure_ascii=True).encode(), "application/json", head)

    def _file(self, path, head=False):
        if not path or not os.path.isfile(path):
            return self._send(404, b"not found", "text/plain; charset=utf-8", head)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"): ctype += "; charset=utf-8"
        with open(path, "rb") as fh: body = fh.read()
        self._send(200, body, ctype, head, cache="no-cache")

    @staticmethod
    def _inside(root, rel):
        f = os.path.normpath(os.path.join(root, rel))
        return f if os.path.normcase(f).startswith(os.path.normcase(os.path.normpath(root)) + os.sep) else None

    # ------------------------------------------------------------ GET
    def do_HEAD(self): self.do_GET(head=True)

    def do_GET(self, head=False):
        p = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if p == "/hub.json":
            try: body, code = self.feed.snapshot(), 200
            except Exception as e: body, code = json.dumps({"error": str(e), "items": []}).encode(), 500
            return self._send(code, body, "application/json", head)
        if p == "/progress.json":
            return self._file(self.store.path, head)
        if p.startswith("/history/"):
            return self._file(self._inside(self.store.history, p[len("/history/"):]), head)
        if p in ("/", "/index.html"):
            return self._file(os.path.join(WEB, "index.html"), head)
        return self._file(self._inside(WEB, p.lstrip("/")), head)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/progress":
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY: raise ValueError("bad body size")
            doc = json.loads(self.rfile.read(n).decode("utf-8"))
            rev = self.store.write(doc)
            self._json(200, {"ok": True, "rev": rev})
        except Stale as e:
            self._json(409, {"ok": False, "error": "stale", "rev": e.current})
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = sys.platform != "win32"   # on Windows SO_REUSEADDR would let two servers share a port
    daemon_threads = True


def make_server(store, feed, port=0, host="127.0.0.1"):
    """Returns a ready server; port 0 picks a free one (read it from ``server.server_address``)."""
    handler = type("BoundHandler", (Handler,), {"store": store, "feed": feed})
    return _Server((host, port), handler)
