# -*- coding: utf-8 -*-
"""Command line.

  python -m crewboard                       serve the hub (same as `serve`)
  python -m crewboard serve [--port N] [--data FILE] [--claude-dir DIR ...] [--demo] [--open]
  python -m crewboard sessions              print what every session and agent is doing
  python -m crewboard add "Title" [--id ID] [--status open|doing|waiting|done] [--area A]
  python -m crewboard note TASK_ID "text"
  python -m crewboard status TASK_ID doing
  python -m crewboard focus TASK_ID [--page PAGE_ID]

Every write goes through the same rule as the page: read the latest file, change it, bump rev by
exactly 1, write. A stale write is retried, never forced.
"""
import argparse, datetime, json, os, re, shutil, sys, tempfile, time

from . import config
from .feed import DemoFeed, Feed
from .server import make_server
from .store import Store, find

EXAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")


def today(): return datetime.date.today().isoformat()


def _store(args):
    s = Store(args.data or config.default_data())
    s.ensure()
    return s


def cmd_serve(args):
    if args.demo:
        tmp = tempfile.mkdtemp(prefix="crewboard-demo-")
        data = os.path.join(tmp, "progress.json")
        shutil.copyfile(os.path.join(EXAMPLES, "progress.json"), data)
        store, feed = Store(data), DemoFeed(os.path.join(EXAMPLES, "sessions.json"))
    else:
        store = _store(args)
        feed = Feed(claude_dirs=config.claude_dirs(args.claude_dir))
    store.snapshot(); store.watch()
    srv = make_server(store, feed, port=args.port)
    url = "http://localhost:%d/" % srv.server_address[1]
    print("crewboard on %s" % url)
    print("  task list  %s" % store.path)
    if not args.demo:
        print("  sessions   %s" % ", ".join(os.path.join(d, "projects") for d in feed.claude_dirs))
        if feed.codex_db: print("  codex      %s" % feed.codex_db)
    sys.stdout.flush()
    if args.open:
        import webbrowser; webbrowser.open(url)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


def cmd_sessions(args):
    feed = Feed(claude_dirs=config.claude_dirs(args.claude_dir))
    items = sorted(json.loads(feed.snapshot())["items"], key=lambda x: -(x.get("last") or 0))
    for it in items[: args.limit]:
        now = ((it.get("now") or {}).get("text") or "")
        pad = "  " if it.get("parent") else ""
        print("%-7s %-8s %s%-44s %s" % (it["src"], it["state"], pad, it["title"][:44 - len(pad)], now[:60]))
    if not items: print("no sessions in the last day under " + ", ".join(feed.claude_dirs))


def _slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return (s or "task") + "-" + time.strftime("%m%d%H%M%S")


def cmd_add(args):
    tid = args.id or _slug(args.title)

    def fn(doc):
        if find(doc.get("items"), tid): raise SystemExit("an item with id %s already exists" % tid)
        it = {"id": tid, "title": args.title, "status": args.status, "added": today(), "due": None, "done": None, "notes": []}
        if args.area: it["area"] = args.area
        doc.setdefault("items", []).append(it)
    print("rev", _store(args).update(fn), "added", tid)


def _need(doc, tid):
    it = find(doc.get("items"), tid)
    if not it: raise SystemExit("no item with id " + tid)
    return it


def cmd_note(args):
    def fn(doc):
        it = _need(doc, args.task)
        notes = it.get("notes") if isinstance(it.get("notes"), list) else ([{"date": "", "text": str(it["notes"])}] if it.get("notes") else [])
        notes.append({"date": today(), "text": args.text}); it["notes"] = notes
    print("rev", _store(args).update(fn))


def cmd_status(args):
    def fn(doc):
        it = _need(doc, args.task)
        it["status"] = args.status; it["done"] = today() if args.status == "done" else None
    print("rev", _store(args).update(fn))


def cmd_focus(args):
    def fn(doc):
        _need(doc, args.task)
        doc["focus"] = {"task": args.task, **({"page": args.page} if args.page else {})}
    print("rev", _store(args).update(fn))


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", help="task list file (default $CREWBOARD_DATA or ~/.crewboard/progress.json)")
    ap = argparse.ArgumentParser(prog="crewboard", description="One screen for every Claude Code session and agent, plus a shared task list.")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", parents=[common], help="serve the hub")
    s.add_argument("--port", type=int, default=int(os.environ.get("CREWBOARD_PORT") or 7788), help="0 picks a free port (default 7788)")
    s.add_argument("--claude-dir", action="append", default=[], help="extra Claude Code config dir; repeat for several accounts")
    s.add_argument("--demo", action="store_true", help="serve the example tasks and example sessions from a temp copy")
    s.add_argument("--open", action="store_true", help="open the page in the default browser")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("sessions", parents=[common], help="print sessions and agents")
    s.add_argument("--claude-dir", action="append", default=[])
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("add", parents=[common], help="add a task"); s.add_argument("title"); s.add_argument("--id")
    s.add_argument("--status", default="open", choices=["open", "doing", "waiting", "done"]); s.add_argument("--area")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("note", parents=[common], help="add a dated note to a task"); s.add_argument("task"); s.add_argument("text"); s.set_defaults(fn=cmd_note)
    s = sub.add_parser("status", parents=[common], help="set a task's status"); s.add_argument("task")
    s.add_argument("status", choices=["open", "doing", "waiting", "done"]); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("focus", parents=[common], help="show a task (or one of its pages) in the Now column"); s.add_argument("task")
    s.add_argument("--page"); s.set_defaults(fn=cmd_focus)

    args = ap.parse_args(argv)
    if not args.cmd:
        args = ap.parse_args(["serve"] + list(argv if argv is not None else sys.argv[1:]))
    args.fn(args)


if __name__ == "__main__":
    main()
