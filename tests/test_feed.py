"""Transcript parsing: Claude Code session and subagent JSONL files become one feed of items."""
import json, os, tempfile, time, unittest
from datetime import datetime, timezone

from crewboard import feed as F


def iso(sec_ago):
    return datetime.fromtimestamp(time.time() - sec_ago, timezone.utc).isoformat().replace("+00:00", "Z")


def user(text, ago, **kw):
    return {"type": "user", "timestamp": iso(ago), "cwd": "/work/acme-web", "message": {"role": "user", "content": text}, **kw}


def assistant(blocks, ago, stop="tool_use", model="claude-opus-5-5"):
    return {"type": "assistant", "timestamp": iso(ago), "message": {"model": model, "content": blocks, "stop_reason": stop}}


def tool(name, inp, tid):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid, ago, content="ok"):
    return {"type": "user", "timestamp": iso(ago), "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records: fh.write(json.dumps(r) + "\n")


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def transcript(self, records, name="s.jsonl"):
        p = os.path.join(self.dir.name, name); write_jsonl(p, records)
        return F.ClaudeTranscript(p)

    def test_working_session(self):
        tr = self.transcript([
            user("Fix the login bug", 120),
            assistant([{"type": "text", "text": "Looking at **auth.py** now."}, tool("Read", {"file_path": "/work/acme-web/auth.py"}, "t1")], 100),
            result("t1", 90),
            assistant([tool("Edit", {"file_path": "/work/acme-web/auth.py", "old_string": "a", "new_string": "b"}, "t2")], 30),
        ])
        self.assertEqual(tr.first[0], "Fix the login bug")
        self.assertEqual(tr.you[0], "Fix the login bug")
        self.assertEqual(tr.reply[0], "Looking at auth.py now.")   # markdown emphasis stripped
        self.assertEqual(tr.now[0], "Editing auth.py")
        self.assertEqual(list(tr.files), ["/work/acme-web/auth.py"])
        self.assertEqual(tr.cwd, "/work/acme-web")
        self.assertEqual(tr.model, "claude-opus-5-5")
        self.assertEqual(tr.state(int(time.time() * 1000), False, False), "working")

    def test_finished_turn_waits_then_goes_idle(self):
        tr = self.transcript([user("hi", 60), assistant([{"type": "text", "text": "Done."}], 50, stop="end_turn")])
        now = int(time.time() * 1000)
        self.assertEqual(tr.state(now, False, False), "waiting")
        self.assertEqual(tr.state(now + 4 * 3600 * 1000, False, False), "idle")
        self.assertEqual(tr.state(now, True, False), "done")   # an agent that ended is finished

    def test_question_to_the_user_is_waiting(self):
        tr = self.transcript([user("plan it", 60), assistant([tool("AskUserQuestion", {"questions": []}, "q1")], 50)])
        self.assertEqual(tr.state(int(time.time() * 1000), False, False), "waiting")
        self.assertEqual(tr.now[0], "Asking you a question")

    def test_interrupt(self):
        tr = self.transcript([user("go", 60), assistant([tool("Bash", {"command": "sleep 99", "description": "Wait"}, "b1")], 50),
                              user("[Request interrupted by user]", 40)])
        self.assertTrue(tr.interrupted)
        self.assertEqual(tr.now[0], "Stopped by you")
        self.assertEqual(tr.state(int(time.time() * 1000), False, False), "idle")

    def test_stale_unfinished_turn_is_stopped(self):
        tr = self.transcript([user("go", 7200), assistant([tool("Read", {"file_path": "x"}, "r")], 7000)])
        self.assertEqual(tr.state(int(time.time() * 1000), False, False), "stopped")
        self.assertEqual(tr.state(int(time.time() * 1000), False, True), "working")   # still alive per the process registry

    def test_todowrite_and_tasks(self):
        tr = self.transcript([user("x", 60), assistant([tool("TodoWrite", {"todos": [{"content": "One", "status": "completed"},
                                                                                      {"content": "Two", "status": "in_progress"}]}, "w")], 50)])
        self.assertEqual(tr.todo_list(), [{"t": "One", "s": "completed"}, {"t": "Two", "s": "in_progress"}])
        tr = self.transcript([user("x", 60), assistant([tool("TaskCreate", {"subject": "Write tests"}, "c1")], 50),
                              result("c1", 45, "Task #3 created"),
                              assistant([tool("TaskUpdate", {"taskId": "3", "status": "completed"}, "u1")], 40)], "t2.jsonl")
        self.assertEqual(tr.todo_list(), [{"t": "Write tests", "s": "completed"}])

    def test_system_text_is_not_a_human_message(self):
        tr = self.transcript([
            user("real question", 90),
            user("<task-notification>agent done</task-notification>", 80),
            user("hidden", 70, isMeta=True),
            result("x", 60),
            user("<system-reminder>ignore</system-reminder>second question", 50),
        ])
        self.assertEqual(tr.first[0], "real question")
        self.assertEqual(tr.you[0], "second question")

    def test_titles(self):
        tr = self.transcript([user("x", 60), {"type": "custom-title", "customTitle": "Login fix"}, {"type": "agent-name", "agentName": "other"}])
        self.assertEqual(tr.title, "Login fix")

    def test_secrets_are_redacted(self):
        key = "sk-" + "a1B2c3D4e5F6g7H8i9J0kLmNoP"
        tr = self.transcript([user("use the key " + key, 60),
                              assistant([tool("Bash", {"command": "export TOKEN=" + "abcdef1234567890"}, "b")], 50)])
        self.assertNotIn(key, tr.you[0]); self.assertIn("[redacted]", tr.you[0])
        self.assertNotIn("abcdef1234567890", tr.now[0])

    def test_incremental_reads(self):
        p = os.path.join(self.dir.name, "grow.jsonl")
        write_jsonl(p, [user("start", 60)])
        tr = F.ClaudeTranscript(p)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(assistant([{"type": "text", "text": "first half"}], 50, stop="end_turn")))   # no newline yet
        tr.poll(); self.assertIsNone(tr.reply)   # a half-written line is held back
        with open(p, "a", encoding="utf-8") as fh: fh.write("\n")
        tr.poll(); self.assertEqual(tr.reply[0], "first half")

    def test_bad_lines_are_skipped(self):
        p = os.path.join(self.dir.name, "bad.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("not json\n" + json.dumps(user("ok", 10)) + "\n{broken\n")
        self.assertEqual(F.ClaudeTranscript(p).you[0], "ok")


class ToolLineTest(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(F.claude_tool_line("Grep", {"pattern": "TODO"}), 'Searching for "TODO"')
        self.assertEqual(F.claude_tool_line("Bash", {"command": "pytest -q\nmore"}), "pytest -q")
        self.assertEqual(F.claude_tool_line("WebFetch", {"url": "https://example.com/a"}), "Reading example.com")
        self.assertEqual(F.claude_tool_line("mcp__github__create_issue", {}), "github: create issue")
        self.assertEqual(F.claude_tool_line("mcp__Claude_Browser__navigate", {"url": "http://localhost:3000"}), "Browser: opening http://localhost:3000")


class FeedTest(unittest.TestCase):
    """A fake Claude Code config dir: projects/<p>/<sid>.jsonl plus subagents and desktop metadata."""
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.dir.name, ".claude")
        self.meta = os.path.join(self.dir.name, "desktop")
        proj = os.path.join(self.cfg, "projects", "-work-acme-web")
        write_jsonl(os.path.join(proj, "s1.jsonl"), [user("Build the thing", 300),
                                                    assistant([tool("Agent", {"description": "Research"}, "a")], 200)])
        sub = os.path.join(proj, "s1", "subagents")
        write_jsonl(os.path.join(sub, "agent-abc.jsonl"), [user("Find prior art", 190),
                                                          assistant([{"type": "text", "text": "Found three."}], 100, stop="end_turn")])
        with open(os.path.join(sub, "agent-abc.meta.json"), "w") as fh: json.dump({"description": "Research prior art", "agentType": "Explore"}, fh)
        write_jsonl(os.path.join(proj, "s2.jsonl"), [user("Old work", 3 * 86400)])
        os.utime(os.path.join(proj, "s2.jsonl"), (time.time() - 3 * 86400,) * 2)   # outside the one-day window
        write_jsonl(os.path.join(proj, "s3.jsonl"), [user("Archived work", 50)])
        os.makedirs(self.meta)
        with open(os.path.join(self.meta, "local_1.json"), "w") as fh:
            json.dump({"cliSessionId": "s3", "isArchived": True, "title": "x"}, fh)
        with open(os.path.join(self.meta, "local_2.json"), "w") as fh:
            json.dump({"cliSessionId": "s1", "title": "Desktop title", "postTurnSummary": {"status_detail": "Halfway"}}, fh)
        self.feed = F.Feed(claude_dirs=[self.cfg], desktop_meta=self.meta, codex_db="")

    def tearDown(self):
        self.dir.cleanup()

    def test_snapshot(self):
        items = {i["id"]: i for i in json.loads(self.feed.snapshot())["items"]}
        self.assertEqual(set(items), {"c:s1", "a:agent-abc"})   # old and archived sessions hidden
        s = items["c:s1"]
        self.assertEqual(s["title"], "Desktop title")
        self.assertEqual(s["summary"], "Halfway")
        self.assertEqual(s["project"], "acme-web")
        self.assertEqual(s["state"], "working")
        self.assertEqual(s["now"]["text"], "Started agent: Research")
        a = items["a:agent-abc"]
        self.assertEqual(a["parent"], "c:s1")
        self.assertEqual(a["kind"], "agent")
        self.assertEqual(a["title"], "Research prior art")
        self.assertEqual(a["state"], "done")
        self.assertEqual(a["task"]["text"], "Find prior art")
        self.assertEqual(a["reply"]["text"], "Found three.")

    def test_snapshot_is_ascii_json(self):
        raw = self.feed.snapshot()
        self.assertTrue(all(b < 128 for b in raw))

    def test_missing_dirs_are_fine(self):
        f = F.Feed(claude_dirs=[os.path.join(self.dir.name, "nope")], desktop_meta=os.path.join(self.dir.name, "nope2"), codex_db="")
        self.assertEqual(json.loads(f.snapshot())["items"], [])

    def test_demo_feed_shifts_times(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = json.loads(F.DemoFeed(os.path.join(here, "crewboard", "examples", "sessions.json")).snapshot())
        first = d["items"][0]
        self.assertGreater(first["last"], d["now"] - 60000)
        self.assertEqual(first["ended"], 0)


class RedactTest(unittest.TestCase):
    def test_patterns(self):
        for s in ("ghp_" + "A" * 30, "AKIA" + "ABCDEFGHIJKLMNOP", "Bearer abcdefghijkl", "password=hunter22hunter", "f" * 40):
            self.assertIn("[redacted]", F.redact(s), s)
        self.assertEqual(F.redact("plain words and a/path/to/file.py"), "plain words and a/path/to/file.py")


if __name__ == "__main__":
    unittest.main()
