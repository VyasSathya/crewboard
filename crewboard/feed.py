# -*- coding: utf-8 -*-
"""Live, read-only feed: what every Claude Code session, background agent (and, if installed, Codex
thread) on this machine is doing. Nothing here writes anywhere, calls a model, or hooks into a session.

Sources (all files the tools already write):
  Claude Code  <config>/projects/<project>/<session>.jsonl                     transcript (tailed)
               <config>/projects/<project>/<session>/subagents/agent-*.jsonl  (+ .meta.json)
               <config>/sessions/<pid>.json                                   live process registry
  Desktop app  <app data>/claude-code-sessions/**/local_*.json                titles / archived flag
  Codex        <codex>/state_*.sqlite (threads table, opened read-only) + the rollout JSONL it points at

Each transcript is read from its tail once, then only the bytes appended since the last poll.
Anything that looks like a key or token is redacted before it leaves this module.
"""
import os, re, json, time, glob, sqlite3, threading, sys
from collections import OrderedDict, deque

from . import config

HOME = config.HOME
WINDOW = 24 * 3600           # show things active in the last day
INIT_TAIL = 768 * 1024       # first read of a transcript: this many bytes from the end
DEEP_TAIL = 6 * 1024 * 1024  # one deeper look if the first read had no human message
MAX_STEP = 24 * 1024 * 1024  # if a file grew more than this between polls, jump to its tail

# ---------------------------------------------------------------- text hygiene
_SECRETS = [
    re.compile(r"\b(?:sk|pk|rk|pa)-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), re.compile(r"\bgithub_pat_\w{20,}"),
    re.compile(r"\bxox[abpr]-[\w-]{10,}"), re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsbp_[A-Za-z0-9]{20,}"), re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}"),
    re.compile(r"(?i)\bbearer\s+[^\s\"']{8,}"), re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\"',]{6,}"),
    re.compile(r"\b[a-fA-F0-9]{32,}\b"),
]
_LONG = re.compile(r"[A-Za-z0-9+/_\-]{40,}={0,2}")


def _long_token(m):
    s = m.group(0)
    if s.count("-") + s.count("_") <= 2 and re.search(r"\d", s) and re.search(r"[a-z]", s) and re.search(r"[A-Z]", s):
        return "[redacted]"
    return s


def redact(s):
    for rx in _SECRETS:
        s = rx.sub(lambda m: (m.group(1) + m.group(2) + "[redacted]") if m.re.groups == 2 else "[redacted]", s)
    return _LONG.sub(_long_token, s)


_TAGS = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|task-notification|ide_[a-z_]+|transcript_delta|environment_context|user_instructions)>.*?</\1>", re.S)


def unmojibake(s):
    if "â€" in s or "Ã" in s:
        try: return s.encode("cp1252").decode("utf-8")
        except Exception:
            for a, b in (("â€™", "'"), ("â€œ", '"'), ("â€”", "-"),
                         ("â€“", "-"), ("â€¦", "..."), ("â€", '"')):
                s = s.replace(a, b)
    return s


def clean(s, n=600):
    """Plain, short, redacted text: no system tags, no markdown emphasis, no secrets."""
    if not isinstance(s, str): return ""
    m = re.search(r"<input>(.*?)</input>", s, re.S)   # delegated voice input: keep what was said
    if m: s = m.group(1)
    s = _TAGS.sub("", unmojibake(s)).strip()
    s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), s)
    s = re.sub(r"(?m)^#{1,6}\s+", "", s); s = re.sub(r"`([^`\n]+)`", r"\1", s)
    s = re.sub(r"[ \t]+", " ", s); s = re.sub(r"\n{3,}", "\n\n", s)
    if len(s) > n: s = s[:n].rstrip() + "..."
    return redact(s)


def short_path(p):
    p = os.path.normpath(str(p or "")) if p else ""
    if p.lower().startswith(HOME.lower()): p = "~" + p[len(HOME):]
    return p


def base(p): return os.path.basename(str(p or "").rstrip("/\\")) or str(p or "")


def ts_ms(iso):
    if not iso: return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception: return 0


# ---------------------------------------------------------------- incremental tail reader
class Tail:
    def __init__(self, path):
        self.path, self.pos, self.buf, self.size = path, None, b"", 0

    def new_records(self):
        try: size = os.path.getsize(self.path)
        except OSError: return []
        if self.pos is None or size < self.pos or size - self.pos > MAX_STEP:
            start, self.buf = max(0, size - INIT_TAIL), b""
            skip_first = start > 0
        else:
            start, skip_first = self.pos, False
        if size == start: self.pos = size; return []
        with open(self.path, "rb") as fh:
            fh.seek(start); data = self.buf + fh.read(size - start)
        self.pos, self.size = size, size
        lines = data.split(b"\n"); self.buf = lines.pop()   # last piece may be a half-written line
        if skip_first and lines: lines = lines[1:]
        out = []
        for l in lines:
            if not l.strip(): continue
            try: out.append(json.loads(l))
            except Exception: pass
        return out

    def read_range(self, start, end):
        out = []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(start); data = fh.read(end - start)
        except OSError: return out
        for l in data.split(b"\n")[1:]:
            try: out.append(json.loads(l))
            except Exception: pass
        return out


def read_head(path, nbytes=262144):
    out = []
    try:
        with open(path, "rb") as fh: data = fh.read(nbytes)
    except OSError: return out
    for l in data.split(b"\n")[:-1] if len(data) == nbytes else data.split(b"\n"):
        try: out.append(json.loads(l))
        except Exception: pass
    return out


# ---------------------------------------------------------------- Claude Code transcript model
def claude_tool_line(name, inp):
    """One plain line for a tool call: "Editing server.py", "Searching for "foo"", ..."""
    inp = inp if isinstance(inp, dict) else {}
    fp = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
    if name in ("Bash", "PowerShell"):
        return inp.get("description") or (str(inp.get("command") or "").strip().splitlines() or [""])[0][:140]
    if name == "Read": return "Reading " + base(fp)
    if name in ("Edit", "MultiEdit", "NotebookEdit"): return "Editing " + base(fp)
    if name == "Write": return "Writing " + base(fp)
    if name == "Grep": return 'Searching for "%s"' % str(inp.get("pattern", ""))[:80]
    if name == "Glob": return "Finding files " + str(inp.get("pattern", ""))[:80]
    if name == "WebSearch": return "Searching the web: " + str(inp.get("query", ""))[:100]
    if name == "WebFetch":
        m = re.match(r"https?://([^/]+)", str(inp.get("url", ""))); return "Reading " + (m.group(1) if m else "a web page")
    if name in ("Agent", "Task"): return "Started agent: " + str(inp.get("description") or inp.get("name") or "")[:100]
    if name == "SendMessage": return "Messaging " + str(inp.get("to", ""))
    if name == "TodoWrite": return "Updating its to-do list"
    if name == "TaskCreate": return "Added task: " + str(inp.get("subject", ""))[:100]
    if name == "TaskUpdate": return "Updated a task"
    if name == "Skill": return "Using skill " + str(inp.get("skill", ""))
    if name == "AskUserQuestion": return "Asking you a question"
    if name == "ExitPlanMode": return "Waiting for you to approve a plan"
    if name == "ToolSearch": return "Loading tools"
    if name == "TaskStop": return "Stopping a task"
    if name.startswith("mcp__"):
        parts = name.split("__"); server, tool = (parts[1] if len(parts) > 1 else ""), (parts[-1] if len(parts) > 2 else "")
        if "browser" in server.lower() or "chrome" in server.lower():
            if tool == "navigate": return "Browser: opening " + str(inp.get("url", ""))[:90]
            if tool == "browser_batch": return "Browser: %d steps" % len(inp.get("actions") or [])
            if tool == "computer":
                a = str(inp.get("action", ""))
                return "Browser: " + {"screenshot": "screenshot", "left_click": "click", "type": "typing", "key": "key press",
                                      "scroll": "scrolling", "zoom": "zoomed screenshot", "wait": "waiting"}.get(a, a.replace("_", " "))
            return "Browser: " + {"javascript_tool": "running a script", "read_page": "reading the page", "get_page_text": "reading the page",
                                  "find": "finding an element", "resize_window": "resizing the window", "form_input": "filling a field",
                                  "read_console_messages": "reading the console", "tabs_create": "new tab", "preview_start": "opening a preview"
                                  }.get(tool, tool.replace("_", " "))
        if re.fullmatch(r"[0-9a-f\-]{30,}", server): server = ""
        return ((server + ": ") if server else "") + tool.replace("_", " ")
    return name


WAIT_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}


class ClaudeTranscript:
    """State of one Claude Code transcript (a session or a subagent), updated incrementally."""
    def __init__(self, path):
        self.tail = Tail(path); self.path = path
        self.title = None; self.first = None; self.started = 0
        self.you = None; self.reply = None; self.now = None; self.pending = {}
        self.ended = False; self.end_ts = 0; self.last_ts = 0; self.cwd = None; self.model = None
        self.todos = None; self.tasks = OrderedDict(); self._task_calls = {}
        self.files = OrderedDict(); self.activity = deque(maxlen=14); self.interrupted = False
        for d in read_head(path):
            if not self.started and d.get("timestamp"): self.started = ts_ms(d["timestamp"])
            h = self._human(d)
            if h: self.first = (h, ts_ms(d.get("timestamp"))); break
        recs = self.tail.new_records()
        if not any(self._human(d) for d in recs) and self.tail.pos and self.tail.pos > INIT_TAIL:
            size = self.tail.pos; recs = self.tail.read_range(max(0, size - DEEP_TAIL), size - INIT_TAIL) + recs
        for d in recs: self.feed(d)

    @staticmethod
    def _human(d):
        """The text a person typed, or None for tool results, meta records and notifications."""
        if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"): return None
        o = d.get("origin")
        if isinstance(o, dict) and o.get("kind") not in (None, "human"): return None
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c): return None
            c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(c, str): return None
        s = c.strip()
        if not s or s.startswith(("<task-notification", "<local-command", "<command-", "Caveat:")): return None
        out = clean(s, 1200)
        return out or None

    def poll(self):
        for d in self.tail.new_records(): self.feed(d)

    def feed(self, d):
        t = d.get("type"); ts = ts_ms(d.get("timestamp"))
        if ts: self.last_ts = max(self.last_ts, ts)
        if d.get("cwd"): self.cwd = d["cwd"]
        if t in ("custom-title", "agent-name"):
            v = d.get("customTitle") or d.get("agentName")
            if v and (t == "custom-title" or not self.title): self.title = v
            return
        if t == "queue-operation" and d.get("operation") == "enqueue" and isinstance(d.get("content"), str):
            s = clean(d["content"], 1200)
            if s and not s.startswith("<"): self.you = (s, ts)
            return
        if t == "user":
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tid = b.get("tool_use_id"); self.pending.pop(tid, None)
                        if tid in self._task_calls:
                            txt = b.get("content"); txt = txt if isinstance(txt, str) else json.dumps(txt)
                            m = re.search(r"#(\d+)", txt or "")
                            if m: self.tasks[m.group(1)] = {"t": self._task_calls.pop(tid), "s": "pending"}
            h = self._human(d)
            if h:
                if h.startswith("[Request interrupted"):
                    self.ended, self.end_ts, self.interrupted = True, ts, True; self.pending.clear()
                    self.now = ("Stopped by you", ts); return
                self.you = (h, ts); self.ended = False; self.interrupted = False
                if not self.first: self.first = (h, ts)
            return
        if t == "assistant":
            m = d.get("message") or {}
            if m.get("model") and not str(m.get("model")).startswith("<"): self.model = m["model"]
            for b in m.get("content") or []:
                if not isinstance(b, dict): continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    self.reply = (clean(b["text"], 900), ts)
                elif b.get("type") == "tool_use":
                    name, inp = b.get("name", ""), b.get("input") or {}
                    line = redact(claude_tool_line(name, inp)); self.pending[b.get("id")] = (name, line, ts)
                    self.now = (line, ts); self.activity.append((ts, line))
                    if name in FILE_TOOLS:
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp: self.files.pop(fp, None); self.files[fp] = ts
                        while len(self.files) > 40: self.files.popitem(last=False)
                    elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                        self.todos = [{"t": clean(x.get("content", ""), 200), "s": x.get("status", "pending")} for x in inp["todos"] if isinstance(x, dict)]
                    elif name == "TaskCreate":
                        self._task_calls[b.get("id")] = clean(inp.get("subject", ""), 200)
                    elif name == "TaskUpdate":
                        k = str(inp.get("taskId", ""))
                        if k in self.tasks:
                            if inp.get("status") == "deleted": self.tasks.pop(k)
                            else:
                                if inp.get("status"): self.tasks[k]["s"] = inp["status"]
                                if inp.get("subject"): self.tasks[k]["t"] = clean(inp["subject"], 200)
            sr = m.get("stop_reason")
            if sr in ("end_turn", "stop_sequence", "max_tokens", "refusal"):
                self.ended, self.end_ts = True, ts; self.pending.clear()
            elif sr == "tool_use" or m.get("content"):
                self.ended = False

    def todo_list(self):
        if self.tasks: return list(self.tasks.values())
        return self.todos or []

    def state(self, now_ms, is_agent, live):
        """working | waiting | idle | done | stopped"""
        age = (now_ms - self.last_ts) / 1000 if self.last_ts else 1e9
        if any(p[0] in WAIT_TOOLS for p in self.pending.values()): return "waiting"
        if not self.ended:
            if age < (1800 if is_agent else 900) or live: return "working"
            return "stopped"
        if is_agent: return "done"
        return "waiting" if age < 3 * 3600 and not self.interrupted else "idle"


# ---------------------------------------------------------------- Codex rollout model (optional)
def codex_cmd_line(cmd):
    if isinstance(cmd, list):
        s = cmd[-1] if cmd else ""
    else: s = str(cmd or "")
    s = s.replace("@'", "").strip()
    lines = [l.strip() for l in s.splitlines() if l.strip() and not l.strip().startswith(("#", "//", "$ErrorActionPreference", "Set-StrictMode", "'@"))]
    for l in lines:   # prefer the first line that runs a real command over variable set-up and code
        m = re.match(r"^(?:\$[\w:]+\s*=\s*)?&?\s*([A-Za-z][\w.-]*(?:\.exe)?)\b(.*)$", l)
        name = m.group(1) if m else ""
        is_call = m and (m.group(2).startswith("(") or ("." in name and not name.lower().endswith((".exe", ".ps1", ".py", ".cmd"))))
        if m and not is_call and not re.match(r"^(const|let|var|function|if|for|foreach|try|catch|return|import|from|text|await|console)$", name):
            return "Ran " + (m.group(1) + m.group(2))[:120]
    return "Ran a script" if lines else "Ran a command"


class CodexRollout:
    def __init__(self, path):
        self.tail = Tail(path); self.path = path
        self.you = None; self.task = None; self.reply = None; self.now = None
        self.working = False; self.end_ts = 0; self.last_ts = 0; self.cwd = None; self.model = None
        self.files = OrderedDict(); self.activity = deque(maxlen=14); self._voice_open = False; self.aborted = False; self.incoming = {}; self.todos = []
        recs = self.tail.new_records()
        has_turn = any(d.get("type") == "event_msg" and (d.get("payload") or {}).get("type") in ("task_started", "task_complete", "turn_aborted") for d in recs)
        steps = sum(1 for d in recs if d.get("type") == "event_msg" and (d.get("payload") or {}).get("type") == "item_completed")
        if (not has_turn or steps < 30 or not any(self._is_user(d) for d in recs)) and self.tail.pos and self.tail.pos > INIT_TAIL:
            size = self.tail.pos; recs = self.tail.read_range(max(0, size - DEEP_TAIL), size - INIT_TAIL) + recs
        for d in recs: self.feed(d)

    @staticmethod
    def _is_user(d):
        p = d.get("payload") or {}
        it = p.get("item") if isinstance(p.get("item"), dict) else {}
        return it.get("type") == "UserMessage" or (p.get("type") == "transcript_segment" and p.get("role") == "user") or p.get("type") == "agent_message"

    def poll(self):
        for d in self.tail.new_records(): self.feed(d)

    def _act(self, line, ts):
        line = redact(line); self.now = (line, ts); self.activity.append((ts, line))

    def feed(self, d):
        t = d.get("type"); p = d.get("payload") or {}; ts = ts_ms(d.get("timestamp"))
        if ts and t != "session_meta": self.last_ts = max(self.last_ts, ts)
        pt = p.get("type")
        if t == "turn_context":
            self.cwd = p.get("cwd") or self.cwd; self.model = p.get("model") or self.model; return
        if t == "event_msg":
            if pt == "task_started": self.working, self.aborted = True, False
            elif pt == "task_complete":
                self.working, self.end_ts = False, ts
                if p.get("last_agent_message"): self.reply = (clean(p["last_agent_message"], 900), ts)
            elif pt == "turn_aborted":
                self.working, self.end_ts, self.aborted = False, ts, True; self._act("Stopped", ts)
            elif pt == "thread_settings_applied":
                self.model = ((p.get("thread_settings") or {}).get("model")) or self.model
            elif pt == "item_completed" and isinstance(p.get("item"), dict):
                self._item(p["item"], ts)
            return
        if t == "realtime_item" and pt == "transcript_segment":
            txt = clean(re.sub(r"\[[a-z ]*\]?", "", p.get("text", "")), 1200)   # drop [inhale], [mouth noise]
            if p.get("role") == "user":
                if len(re.findall(r"[A-Za-z']{2,}", txt)) < 2: return          # a breath or a stray syllable
                if self._voice_open and self.you and ts - self.you[1] < 120000:
                    self.you = (clean(self.you[0] + " " + txt, 1200), ts)
                else: self.you = (txt, ts)
                self._voice_open = True
            else: self._voice_open = False
            return
        if t == "response_item" and pt == "function_call" and p.get("name") == "update_plan":
            try:
                plan = json.loads(p.get("arguments") or "{}").get("plan") or []
                self.todos = [{"t": clean(x.get("step", ""), 200), "s": x.get("status", "pending")} for x in plan if isinstance(x, dict)]
            except Exception: pass
            return
        if t == "response_item" and pt == "agent_message":
            txt = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
            txt = re.sub(r"^Message Type:[^\n]*\n(Task name:[^\n]*\n)?", "", txt)
            self.incoming[p.get("recipient") or ""] = (clean(txt, 900), ts)
            return

    def _item(self, it, ts):
        ty = it.get("type")
        if ty == "UserMessage":
            txt = "\n".join(c.get("text", "") for c in it.get("content") or [] if isinstance(c, dict))
            s = clean(txt, 1200)
            if s: self.you = (s, ts); self._voice_open = False
        elif ty == "AgentMessage":
            txt = "\n".join(c.get("text", "") for c in it.get("content") or [] if isinstance(c, dict))
            if txt.strip(): self.reply = (clean(txt, 900), ts)
        elif ty == "CommandExecution": self._act(codex_cmd_line(it.get("command")), ts)
        elif ty == "FileChange":
            ch = it.get("changes") or {}
            for fp in ch:
                self.files.pop(fp, None); self.files[fp] = ts
            while len(self.files) > 40: self.files.popitem(last=False)
            if ch: self._act("Editing " + ", ".join(base(f) for f in list(ch)[:3]), ts)
        elif ty == "McpToolCall": self._act("%s: %s" % (it.get("server", ""), str(it.get("tool", "")).replace("_", " ")), ts)
        elif ty == "Extension":
            q = it.get("query") or ((it.get("action") or {}).get("url"))
            self._act("Web: " + str(q or it.get("kind", ""))[:120], ts)
        elif ty == "SubAgentActivity":
            ap = str(it.get("agent_path", ""))
            who = "the main thread" if ap.strip("/") == "root" else base(ap).replace("_", " ")
            verb = {"interacted": "Talking with", "spawned": "Started", "completed": "Heard back from", "closed": "Closed"}.get(it.get("kind"), "Working with")
            self._act("%s %s" % (verb, who), ts)
        elif ty == "CollabAgentToolCall": self._act("Coordinating agents", ts)
        elif ty == "ImageView": self._act("Looking at " + base(it.get("path", "")), ts)
        elif ty == "ContextCompaction": self._act("Compacting its context", ts)

    def state(self, now_ms, is_agent):
        age = (now_ms - self.last_ts) / 1000 if self.last_ts else 1e9
        if self.working:
            return "working" if age < 1800 else "stopped"
        if is_agent: return "done"
        return "waiting" if age < 3 * 3600 and not self.aborted else "idle"


# ---------------------------------------------------------------- process check (never os.kill a pid on Windows)
def pid_alive(pid):
    try: pid = int(pid)
    except (TypeError, ValueError): return False
    if pid <= 0: return False
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h: return False
            code = ctypes.c_ulong(); ok = k.GetExitCodeProcess(h, ctypes.byref(code)); k.CloseHandle(h)
            return bool(ok) and code.value == 259   # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)   # signal 0: existence check only
        return True
    except PermissionError:
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- the feed
class Feed:
    def __init__(self, claude_dirs=None, desktop_meta=None, codex_db=None, window=WINDOW):
        self.claude_dirs = claude_dirs if claude_dirs is not None else config.claude_dirs()
        self.desktop_meta = desktop_meta if desktop_meta is not None else config.desktop_meta_dir()
        self.codex_db = codex_db if codex_db is not None else config.codex_db()
        self.window = window
        self.scratch = config.scratch_markers()
        self.lock = threading.Lock()
        self.claude = {}; self.codex = {}; self.meta_cache = {}; self.submeta = {}
        self.cache = (0, b"{}")

    def _desktop_meta(self, now):
        out = {}
        if not self.desktop_meta: return out
        for f in glob.glob(os.path.join(self.desktop_meta, "**", "local_*.json"), recursive=True):
            try: mt = os.path.getmtime(f)
            except OSError: continue
            c = self.meta_cache.get(f)
            if not c or c[0] != mt:
                if now - mt > 30 * 86400: self.meta_cache[f] = (mt, None); continue
                try:
                    with open(f, encoding="utf-8") as fh: d = json.load(fh)
                    pts = d.get("postTurnSummary") or {}
                    v = {"title": d.get("title"), "archived": bool(d.get("isArchived")), "detail": pts.get("status_detail"),
                         "needs": pts.get("needs_action"),
                         "ids": [d.get("cliSessionId")] + list(d.get("priorCliSessionIds") or [])}
                except Exception: v = None
                c = self.meta_cache[f] = (mt, v)
            if c[1]:
                for sid in c[1]["ids"]:
                    if sid: out[sid] = c[1]
        return out

    def _live(self):
        out = {}
        for cd in self.claude_dirs:
            for f in glob.glob(os.path.join(cd, "sessions", "*.json")):
                try:
                    with open(f, encoding="utf-8") as fh: d = json.load(fh)
                    if d.get("sessionId") and pid_alive(d.get("pid")): out[d["sessionId"]] = d
                except Exception: pass
        return out

    def _scratch(self, cwd):
        c = os.path.normcase(os.path.normpath(cwd)) + os.sep if cwd else ""
        return any(c.startswith(m) for m in self.scratch)

    def _claude_items(self, now):
        now_ms = int(now * 1000); items = []
        meta = self._desktop_meta(now); live = self._live()
        projects = []
        for cd in self.claude_dirs:
            try: projects += [e.path for e in os.scandir(os.path.join(cd, "projects")) if e.is_dir()]
            except OSError: pass
        seen = set()
        for proj in projects:
            try: entries = list(os.scandir(proj))
            except OSError: continue
            for e in entries:
                if not e.name.endswith(".jsonl") or not e.is_file(): continue
                sid = e.name[:-6]
                try: mt = e.stat().st_mtime
                except OSError: continue
                if now - mt > self.window and sid not in live: continue
                m = meta.get(sid)
                if m and m["archived"]: continue
                tr = self.claude.get(e.path)
                if tr is None: tr = self.claude[e.path] = ClaudeTranscript(e.path)
                else: tr.poll()
                cwd = tr.cwd or ""
                if self._scratch(cwd) and sid not in live and not m: continue
                seen.add(e.path)
                title = (m and m.get("title")) or tr.title or (tr.first and tr.first[0].splitlines()[0][:90]) or "Untitled session"
                it = self._claude_item(tr, "c:" + sid, title, now_ms, False, sid in live)
                it["project"] = base(cwd) if cwd else base(proj)
                if m and m.get("detail"): it["summary"] = clean(m["detail"], 300)
                if m and m.get("needs"): it["needs"] = clean(m["needs"], 300)
                items.append(it)
                # background agents of this session
                sub = os.path.join(proj, sid, "subagents")
                try: subs = [s for s in os.scandir(sub) if s.name.endswith(".jsonl")]
                except OSError: subs = []
                names = {}
                for s in subs:
                    try: smt = s.stat().st_mtime
                    except OSError: continue
                    if now - smt > self.window: continue
                    at = self.claude.get(s.path)
                    if at is None: at = self.claude[s.path] = ClaudeTranscript(s.path)
                    else: at.poll()
                    seen.add(s.path)
                    md = self.submeta.get(s.path)
                    if md is None:
                        try:
                            with open(s.path[:-6] + ".meta.json", encoding="utf-8") as fh: md = json.load(fh)
                        except Exception: md = {}
                        self.submeta[s.path] = md
                    aname = md.get("description") or md.get("name") or (at.first and at.first[0][:80]) or "Agent"
                    ai = self._claude_item(at, "a:" + s.name[:-6], aname, now_ms, True, False)
                    ai["parent"] = "c:" + sid; ai["handle"] = md.get("name"); ai["project"] = it["project"]
                    ai["agentType"] = md.get("agentType")
                    items.append(ai); names[s.name[6:-6]] = md.get("name") or aname
                # "Messaging a6f75baf..." reads better as "Messaging the agent's name"
                for x in items:
                    if x.get("id") == it["id"] or x.get("parent") == it["id"]:
                        for a in [x.get("now")] + (x.get("activity") or []):
                            if a and a.get("text", "").startswith("Messaging "):
                                k = a["text"][10:]; a["text"] = "Messaging " + names.get(k, k)
        for k in [k for k in self.claude if k not in seen]: self.claude.pop(k, None)
        return items

    def _claude_item(self, tr, iid, title, now_ms, is_agent, live):
        st = tr.state(now_ms, is_agent, live)
        now_line = tr.now
        if st in ("waiting", "idle", "done") and not any(p[0] in WAIT_TOOLS for p in tr.pending.values()):
            now_line = None
        you = tr.you
        it = {"id": iid, "src": "claude", "kind": "agent" if is_agent else "session", "title": clean(title, 140),
              "state": st, "last": tr.last_ts, "started": tr.started or (tr.first and tr.first[1]) or 0,
              "ended": tr.end_ts if tr.ended else 0, "model": tr.model, "live": live,
              "now": {"text": now_line[0], "ts": now_line[1]} if now_line else None,
              "reply": {"text": tr.reply[0], "ts": tr.reply[1]} if tr.reply else None,
              "todos": tr.todo_list(),
              "files": [{"p": short_path(p), "ts": t} for p, t in reversed(list(tr.files.items())[-14:])],
              "activity": [{"ts": a, "text": b} for a, b in reversed(tr.activity)]}
        if is_agent:
            if tr.first: it["task"] = {"text": tr.first[0], "ts": tr.first[1]}
            if you and tr.first and you[0] != tr.first[0]: it["you"] = {"text": you[0], "ts": you[1]}
        elif you: it["you"] = {"text": you[0], "ts": you[1]}
        return it

    def _codex_items(self, now):
        if not self.codex_db or not os.path.isfile(self.codex_db): return []
        now_ms = int(now * 1000); items = []
        rows = []
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.codex_db.replace("\\", "/"), uri=True, timeout=0.5)
            try:
                rows = con.execute("select id, rollout_path, created_at_ms, updated_at_ms, cwd, title, name, first_user_message, "
                                   "agent_nickname, agent_path, thread_source, source, model from threads "
                                   "where archived=0 and updated_at_ms > ? order by updated_at_ms desc limit 80",
                                   (now_ms - self.window * 1000,)).fetchall()
            finally: con.close()
        except Exception:
            rows = []
        seen = set()
        for (tid, rp, created, updated, cwd, title, name, fum, nick, apath, tsrc, source, model) in rows:
            rp = rp or ""
            if rp.startswith("\\\\?\\"): rp = rp[4:]
            if not rp or not os.path.isfile(rp): continue
            ro = self.codex.get(rp)
            if ro is None: ro = self.codex[rp] = CodexRollout(rp)
            else: ro.poll()
            seen.add(rp)
            parent = None
            try:
                sj = json.loads(source) if source and source.startswith("{") else {}
                parent = (((sj.get("subagent") or {}).get("thread_spawn") or {}).get("parent_thread_id"))
            except Exception: pass
            is_agent = tsrc == "subagent" or bool(parent)
            if is_agent:
                t = base(apath or "").replace("_", " ").strip().capitalize() or "Agent"
            else:
                t = name or title or ((fum or "").strip().splitlines() or ["Codex thread"])[0][:90]
            st = ro.state(now_ms, is_agent)
            now_line = ro.now if st == "working" else None
            cwd = (cwd or "")
            if cwd.startswith("\\\\?\\"): cwd = cwd[4:]
            it = {"id": "x:" + tid, "src": "codex", "kind": "agent" if is_agent else "session", "title": clean(t, 140),
                  "state": st, "last": max(ro.last_ts, 0) or (updated or 0), "started": created or 0,
                  "ended": ro.end_ts if not ro.working else 0, "model": ro.model or model, "live": False,
                  "project": base(cwd),
                  "now": {"text": now_line[0], "ts": now_line[1]} if now_line else None,
                  "reply": {"text": ro.reply[0], "ts": ro.reply[1]} if ro.reply else None,
                  "todos": ro.todos,
                  "files": [{"p": short_path(p), "ts": x} for p, x in reversed(list(ro.files.items())[-14:])],
                  "activity": [{"ts": a, "text": b} for a, b in reversed(ro.activity)]}
            if is_agent:
                it["parent"] = "x:" + parent if parent else None; it["handle"] = nick
                inc = ro.incoming.get(apath or "")
                cand = [x for x in (ro.you, inc) if x]
                if cand:
                    x = max(cand, key=lambda v: v[1]); it["task"] = {"text": x[0], "ts": x[1]}
            elif ro.you: it["you"] = {"text": ro.you[0], "ts": ro.you[1]}
            items.append(it)
        for k in [k for k in self.codex if k not in seen]: self.codex.pop(k, None)
        return items

    def items(self, now=None):
        now = now or time.time(); items = []
        for fn in (self._claude_items, self._codex_items):
            try: items += fn(now)
            except Exception as e:
                items.append({"id": "err:" + fn.__name__, "src": "error", "kind": "session", "title": "Feed error", "state": "idle",
                              "last": int(now * 1000), "reply": {"text": clean(str(e), 300), "ts": int(now * 1000)}})
        return items

    def snapshot(self):
        """Returns the JSON bytes; recomputed at most every 1.2 s however many tabs poll."""
        with self.lock:
            now = time.time()
            if now - self.cache[0] < 1.2: return self.cache[1]
            out = json.dumps({"now": int(now * 1000), "items": self.items(now)}, ensure_ascii=True).encode()
            self.cache = (now, out)
            return out


class DemoFeed:
    """Serves a fixed list of sessions (examples/sessions.json) with timestamps shifted to now, so the
    demo looks alive without reading anyone's transcripts."""
    def __init__(self, path):
        with open(path, encoding="utf-8") as fh: self.items = json.load(fh)["items"]

    def snapshot(self):
        now = int(time.time() * 1000)

        def shift(x):
            if isinstance(x, dict):
                return {k: (now - v * 1000 if k in ("ts", "last", "started", "ended") and isinstance(v, (int, float)) and v > 0 and v < 1e8 else shift(v))
                        for k, v in x.items()}
            if isinstance(x, list): return [shift(v) for v in x]
            return x
        return json.dumps({"now": now, "items": shift(self.items)}, ensure_ascii=True).encode()
