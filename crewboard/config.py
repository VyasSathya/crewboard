# -*- coding: utf-8 -*-
"""Where things live. Everything is auto-detected; every path can be overridden by a flag or an
environment variable, so nothing is hardcoded to one machine.

  Claude Code config dir   $CLAUDE_CONFIG_DIR, else ~/.claude          (projects/, sessions/)
  Claude desktop app data  Windows  %APPDATA%/Claude
                           macOS    ~/Library/Application Support/Claude
                           Linux    $XDG_CONFIG_HOME/Claude or ~/.config/Claude
  Codex (optional)         $CODEX_HOME, else ~/.codex                   (state_*.sqlite)
  Task list                $CREWBOARD_DATA, else ~/.crewboard/progress.json
"""
import glob, os, sys, tempfile

HOME = os.path.expanduser("~")


def claude_dirs(extra=None):
    """Claude Code config dirs to read. Extra dirs (``--claude-dir``, or ``CREWBOARD_CLAUDE_DIRS``
    separated by os.pathsep) are added to the default one, so several accounts show on one screen."""
    out = []
    env = os.environ.get("CREWBOARD_CLAUDE_DIRS")
    for d in (extra or []) + (env.split(os.pathsep) if env else []):
        if d: out.append(os.path.abspath(os.path.expanduser(d)))
    if not out:
        out.append(os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"))))
    seen, uniq = set(), []
    for d in out:
        k = os.path.normcase(d)
        if k not in seen: seen.add(k); uniq.append(d)
    return uniq


def desktop_app_dir():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming"), "Claude")
    if sys.platform == "darwin":
        return os.path.join(HOME, "Library", "Application Support", "Claude")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "Claude")


def desktop_meta_dir():
    """The Claude desktop app keeps per-session titles and an archived flag here (optional)."""
    return os.environ.get("CREWBOARD_DESKTOP_META") or os.path.join(desktop_app_dir(), "claude-code-sessions")


def codex_db():
    """Newest Codex state database, or None when Codex is not installed."""
    if os.environ.get("CREWBOARD_NO_CODEX"): return None
    home = os.path.expanduser(os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex"))
    found = sorted(glob.glob(os.path.join(home, "state_*.sqlite")),
                   key=lambda p: int("".join(c for c in os.path.basename(p) if c.isdigit()) or 0))
    return found[-1] if found else None


def default_data():
    return os.path.abspath(os.path.expanduser(os.environ.get("CREWBOARD_DATA") or os.path.join(HOME, ".crewboard", "progress.json")))


def scratch_markers():
    """Sessions whose working directory is under one of these are headless probes (Claude Code
    scratchpads); they are hidden unless still running."""
    marks = {os.path.join(tempfile.gettempdir(), "claude"), "/tmp/claude"}
    return [os.path.normcase(os.path.normpath(m)) + os.sep for m in marks]
