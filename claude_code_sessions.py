#!/usr/bin/env python3
"""Claude Code Sessions — a small libadwaita/GTK4 dashboard.

Lists every Claude Code session found under ~/.claude/projects (newest first),
shows its working directory, and lets you resume it in a terminal — or start a
new named session in any directory.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"
APP_ID = "io.github.jerem.ClaudeCodeSessions"
# Our own state (starred sessions). Maps to ~/.config outside the sandbox and to
# ~/.var/app/<id>/config inside it — writable in both cases.
STAR_PATH = (Path(GLib.get_user_config_dir())
             / "claude-code-sessions" / "starred.json")
# Parsed-session cache. Regenerable, so it lives in the cache dir.
INDEX_PATH = (Path(GLib.get_user_cache_dir())
              / "claude-code-sessions" / "index.json")


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[a-z0-9_]+")


def _typo_budget(term):
    """How many typos to forgive in a term. Short terms get none: at three
    characters, one edit reaches half the dictionary."""
    if len(term) < 4:
        return 0
    return 1 if len(term) < 8 else 2


def _near(term, word, budget):
    """True if `term` is within `budget` Damerau-Levenshtein edits of `word`.

    Banded DP: an alignment that strays more than `budget` cells off the
    diagonal has already spent more than the budget, so each row costs
    O(budget) instead of O(len(word)). The transposition case (the `prev2`
    row) is what lets "cluade" reach "claude" in a single edit — plain
    Levenshtein scores swapped letters as two.
    """
    n, m = len(term), len(word)
    if abs(n - m) > budget:
        return False
    over = budget + 1                      # any value at/above this is a reject
    prev2 = None
    prev = list(range(m + 1))              # edits from "" to each word prefix
    for i in range(1, n + 1):
        lo, hi = max(1, i - budget), min(m, i + budget)
        cur = [over] * (m + 1)
        if lo == 1:
            cur[0] = i
        c = term[i - 1]
        row_best = over
        for j in range(lo, hi + 1):
            cost = min(prev[j] + 1,                        # delete
                       cur[j - 1] + 1,                     # insert
                       prev[j - 1] + (c != word[j - 1]))   # keep / substitute
            if (i > 1 and j > 1
                    and c == word[j - 2] and term[i - 2] == word[j - 1]):
                cost = min(cost, prev2[j - 2] + 1)         # transpose
            cur[j] = cost
            if cost < row_best:
                row_best = cost
        if row_best > budget:
            return False                   # every alignment is already over
        prev2, prev = prev, cur
    return prev[m] <= budget


class Query:
    """The search box's text, matched against sessions.

    Every whitespace-separated term has to hit (AND), and a term hits when it
    appears verbatim anywhere in a session's blob — title, directory, id or
    conversation. A term that hits *nothing* verbatim is taken to be a typo and
    corrected to the near-spelled words the sessions actually contain, and then
    searched as if you had spelled it right — "sesion" or "flatpk" find exactly
    what "session" and "flatpak" would, instead of coming up empty.

    Correctly spelled queries therefore behave exactly as before: the typo
    pass only ever adds results to a search that was about to return none.
    """

    def __init__(self, text, sessions):
        self.terms = text.lower().split()
        self.near = {}          # typo'd term -> corrected spellings to accept
        missing = [t for t in self.terms
                   if not any(t in s.search_blob for s in sessions)]
        if missing and sessions:
            vocab = frozenset().union(*(s.tokens for s in sessions))
            for term in missing:
                budget = _typo_budget(term)
                if not budget:
                    continue
                near = frozenset(w for w in vocab if _near(term, w, budget))
                if near:
                    self.near[term] = near

    def keeps(self, session):
        for term in self.terms:
            if term in session.search_blob:
                continue
            near = self.near.get(term)
            if near and any(w in session.search_blob for w in near):
                continue
            return False
        return True


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
class Session:
    __slots__ = ("session_id", "cwd", "title", "mtime", "path", "search_blob",
                 "_tokens")

    def __init__(self, session_id, cwd, title, mtime, path, search_blob):
        self.session_id = session_id
        self.cwd = cwd
        self.title = title
        self.mtime = mtime
        self.path = path
        # Lowercased haystack: title + dir + id + the user-side conversation.
        self.search_blob = search_blob
        self._tokens = None

    @property
    def tokens(self):
        """The blob as distinct words: the vocabulary a typo'd term is corrected
        against. Built on first use — only a misspelled search needs it, and
        splitting every blob on every rescan is not free."""
        if self._tokens is None:
            self._tokens = frozenset(_WORD_RE.findall(self.search_blob))
        return self._tokens


def _first_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
    return None


PROMPT_CAP = 200_000   # index the whole conversation; just bound runaway logs


def _json_get(raw, key):
    """Read one key out of a JSONL line, or None if the line won't parse."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return value.get(key) if isinstance(value, dict) else None


def _user_text(raw):
    """The user-typed text of a user line, or None if it isn't one."""
    msg = _json_get(raw, "message")
    txt = _first_text(msg.get("content")) if isinstance(msg, dict) else None
    if not txt:
        return None
    txt = txt.strip()
    # Skip tool results / injected context (they start with '<').
    return txt if txt and not txt.startswith("<") else None


def _blank_state():
    return {"offset": 0, "cwd": None, "ai_title": None, "custom_title": None,
            "first_prompt": None, "prompts": "", "prompt_chars": 0}


def _read_into(path, state):
    """Fold the not-yet-read lines of `path` into `state`.

    Read as bytes and substring-test each line before parsing it, so the big
    assistant payloads are never decoded — only the handful of lines carrying
    metadata. Parsing resumes at the offset the last pass stopped on, which is
    what keeps a 350 MB log that just gained a few KB costing a few KB.
    """
    last_cwd_line = None
    fresh = []
    with open(path, "rb") as fh:
        fh.seek(state["offset"])
        for raw in fh:
            if not raw.endswith(b"\n"):
                break          # half-written line; pick it up on the next pass
            state["offset"] += len(raw)
            # Keep the *last* cwd: `/cd` changes it mid-session, and the session
            # file moves to the new project dir, so the current cwd is what
            # `claude --resume` needs (parse just this one line, at the end).
            if b'"cwd"' in raw:
                last_cwd_line = raw
            if b'"aiTitle"' in raw:
                state["ai_title"] = _json_get(raw, "aiTitle") or state["ai_title"]
            if b'"customTitle"' in raw:
                state["custom_title"] = (_json_get(raw, "customTitle")
                                         or state["custom_title"])
            if (state["prompt_chars"] < PROMPT_CAP
                    and (b'"type":"user"' in raw or b'"type": "user"' in raw)):
                txt = _user_text(raw)
                if txt:
                    if state["first_prompt"] is None:
                        state["first_prompt"] = txt
                    fresh.append(txt)
                    state["prompt_chars"] += len(txt)
    if last_cwd_line is not None:
        state["cwd"] = _json_get(last_cwd_line, "cwd") or state["cwd"]
    if fresh:
        state["prompts"] = " ".join(x for x in (state["prompts"], *fresh) if x)


def parse_session(path, index=None):
    """Build a Session from a .jsonl, reusing whatever the index already knows."""
    try:
        st = path.stat()
    except OSError:
        return None
    state = index.resume(path, st) if index is not None else None
    if state is None:
        state = _blank_state()
    if state["offset"] < st.st_size:
        try:
            _read_into(path, state)
        except OSError:
            return None
    if index is not None:
        index.store(path, st, state)

    cwd = state["cwd"]
    if cwd is None:
        # Fall back to decoding the directory name (slashes were turned to '-').
        cwd = "/" + path.parent.name.lstrip("-").replace("-", "/")

    # A user's /rename (custom-title) wins over the auto-generated ai-title.
    display_title = (state["custom_title"] or state["ai_title"]
                     or state["first_prompt"] or "(untitled session)")
    if len(display_title) > 90:
        display_title = display_title[:89] + "…"

    blob = " ".join([state["custom_title"] or "", state["ai_title"] or "",
                     cwd, path.stem, state["prompts"]]).lower()

    return Session(
        session_id=path.stem,
        cwd=cwd,
        title=display_title,
        mtime=st.st_mtime,
        path=str(path),
        search_blob=blob,
    )


class Index:
    """Cache of parsed session state, keyed by file path.

    Rescans are frequent — the file monitor fires on every write to the session
    you are sitting in — and re-reading every log each time got expensive once
    they grew to hundreds of megabytes. Session logs only ever grow, so each
    entry keeps the byte offset parsing stopped at: an unchanged file then costs
    a stat(), and a grown one costs only its new bytes. It survives restarts, so
    a cold start after the first is a stat() per file too.
    """

    VERSION = 1

    def __init__(self):
        self.entries = {}
        self._dirty = False
        self._saved_at = 0.0
        try:
            data = json.loads(INDEX_PATH.read_text())
            if data.get("version") == self.VERSION:
                self.entries = data.get("entries") or {}
        except (OSError, ValueError):
            pass

    def resume(self, path, st):
        """The state to carry on from, or None to parse `path` from scratch."""
        entry = self.entries.get(str(path))
        if entry is None or st.st_size < entry.get("offset", 0):
            return None     # unknown, or rewritten shorter: our offset is a lie
        return entry

    def store(self, path, st, state):
        state["size"] = st.st_size
        state["mtime"] = st.st_mtime
        self.entries[str(path)] = state
        self._dirty = True

    def forget_missing(self, seen):
        if len(seen) != len(self.entries):
            self.entries = {p: e for p, e in self.entries.items() if p in seen}
            self._dirty = True

    def save(self, force=False):
        """Persist the index. Throttled: writing it on every rescan during an
        active session would hand back the time the index just saved."""
        if not self._dirty:
            return
        if not force and time.monotonic() - self._saved_at < 60:
            return
        try:
            INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = INDEX_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": self.VERSION,
                                       "entries": self.entries}))
            tmp.replace(INDEX_PATH)      # atomic swap
        except (OSError, ValueError):
            return
        self._dirty = False
        self._saved_at = time.monotonic()


def scan_sessions(index=None):
    sessions = []
    seen = set()
    if not PROJECTS_DIR.is_dir():
        return sessions
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        seen.add(str(jsonl))
        s = parse_session(jsonl, index)
        if s is not None:
            sessions.append(s)
    if index is not None:
        index.forget_missing(seen)
        index.save()
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


def human_age(mtime):
    delta = max(0, time.time() - mtime)
    if delta < 60:
        return "just now"
    mins = delta / 60
    if mins < 60:
        return f"{int(mins)} min ago"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)} h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)} d ago"
    if days < 30:
        return f"{int(days / 7)} w ago"
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


class Stars:
    """Persistent set of starred session ids, stored as a small JSON file."""

    def __init__(self):
        self.ids = set()
        try:
            data = json.loads(STAR_PATH.read_text())
            if isinstance(data, list):
                self.ids = set(data)
        except (OSError, json.JSONDecodeError):
            pass

    def is_starred(self, session_id):
        return session_id in self.ids

    def set(self, session_id, starred):
        if starred:
            self.ids.add(session_id)
        else:
            self.ids.discard(session_id)
        self._save()

    def _save(self):
        try:
            STAR_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STAR_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self.ids)))
            tmp.replace(STAR_PATH)  # atomic swap
        except OSError:
            pass


def rename_session(session, new_title):
    """Persist a custom name by appending the same entry `/rename` writes."""
    entry = {"type": "custom-title",
             "customTitle": new_title,
             "sessionId": session.session_id}
    with open(session.path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- #
# Terminal launching
# --------------------------------------------------------------------------- #
def in_flatpak():
    return os.path.exists("/.flatpak-info")


def on_host(argv, **kwargs):
    """Spawn argv on the host when sandboxed, otherwise directly."""
    if in_flatpak():
        argv = ["flatpak-spawn", "--host", *argv]
    return subprocess.Popen(argv, **kwargs)


def host_has(name):
    """Is `name` an executable available where the terminal will be launched?"""
    if in_flatpak():
        try:
            r = subprocess.run(
                ["flatpak-spawn", "--host", "sh", "-c",
                 f"command -v {shlq(name)}"],
                capture_output=True, timeout=5)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return shutil.which(name) is not None


def host_env(var):
    if in_flatpak():
        try:
            r = subprocess.run(["flatpak-spawn", "--host", "printenv", var],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None
    return os.environ.get(var)


def host_isdir(path):
    """Does `path` exist as a directory where the terminal will be launched?

    Always ask the host when sandboxed: we only mount ~/.claude, so a project
    directory reads as missing from in here even when it is really there.
    """
    if in_flatpak():
        try:
            r = subprocess.run(
                ["flatpak-spawn", "--host", "sh", "-c",
                 f"test -d {shlq(path)}"],
                capture_output=True, timeout=5)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return os.path.isdir(path)


def host_mkdir(path):
    """Create `path` and any missing parents on the host.

    Returns None on success, else a message. Done host-side for the same reason
    as host_isdir: the sandbox can't write outside ~/.claude, but the host
    process we're allowed to spawn can.
    """
    if in_flatpak():
        try:
            r = subprocess.run(
                ["flatpak-spawn", "--host", "mkdir", "-p", str(path)],
                capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return None
            return r.stderr.strip().split(": ")[-1] or "mkdir failed"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return exc.strerror or str(exc)
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name):
    """Turn a session name into a directory name: "My Café App" -> my-cafe-app."""
    ascii_name = (unicodedata.normalize("NFKD", name)
                  .encode("ascii", "ignore").decode())
    return _SLUG_RE.sub("-", ascii_name.lower()).strip("-")


_DOC_PORTAL_RE = re.compile(r"^/run/user/\d+/doc/([^/]+)/")


def host_path(path):
    """Translate a document-portal path back to the real path on the host.

    The file chooser doesn't hand a sandboxed app the directory you picked: it
    exports it through the Documents portal and returns a
    /run/user/UID/doc/DOCID/name mount instead, which is meaningless to the
    terminal we spawn on the host — and granting --filesystem doesn't stop it.
    Only the host can resolve it back: the portal's own Info method answers
    "Not allowed in sandbox", so ask the flatpak CLI out there instead.
    """
    m = _DOC_PORTAL_RE.match(path.rstrip("/") + "/")
    if not m or not in_flatpak():
        return path
    doc_id = m.group(1)
    try:
        r = subprocess.run(
            ["flatpak-spawn", "--host", "flatpak", "documents",
             "--columns=id,origin"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return path
    if r.returncode != 0:
        return path
    for line in r.stdout.splitlines():
        row = line.split("\t")
        if len(row) == 2 and row[0].strip() == doc_id:
            real = row[1].strip()
            if not real:
                break
            # Anything below the exported entry itself carries over unchanged.
            tail = path.rstrip("/").split("/")[7:]
            return os.path.join(real, *tail) if tail else real
    return path


def claude_bin():
    # On the host (incl. via flatpak-spawn) a login shell's PATH resolves it.
    if in_flatpak():
        return "claude"
    return shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


# Per-terminal argv templates. {cwd} is filled in; the trailing entries are the
# command to run. Only used as a fallback when no system default is configured.
KNOWN_TERMINALS = [
    ("ghostty", lambda cwd: ["ghostty", f"--working-directory={cwd}", "-e"]),
    ("ptyxis", lambda cwd: ["ptyxis", "--new-window",
                            f"--working-directory={cwd}", "--"]),
    ("kgx", lambda cwd: ["kgx", "--working-directory", cwd, "--"]),
    ("gnome-terminal", lambda cwd: ["gnome-terminal",
                                    f"--working-directory={cwd}", "--"]),
    ("konsole", lambda cwd: ["konsole", "--workdir", cwd, "-e"]),
    ("kitty", lambda cwd: ["kitty", "-d", cwd]),
    ("alacritty", lambda cwd: ["alacritty", "--working-directory", cwd, "-e"]),
    ("foot", lambda cwd: ["foot", "-D", cwd]),
    ("wezterm", lambda cwd: ["wezterm", "start", "--cwd", cwd, "--"]),
    ("xterm", lambda cwd: ["xterm", "-e"]),
]


def open_terminal(cwd, claude_args):
    """Open the user's default terminal in `cwd` running `claude claude_args`.

    Resolution order:
      1. $TERMINAL              (explicit user override)
      2. xdg-terminal-exec      (the freedesktop default-terminal standard)
      3. first installed terminal we know how to drive (ghostty, ptyxis, …)
    """
    # Login shell so PATH/nvm/etc. are set up. cd explicitly (a flatpak-spawned
    # host process starts in $HOME, not our cwd), then drop to an interactive
    # shell once Claude exits so the window stays open.
    claude = " ".join([shlq(claude_bin()), *(shlq(a) for a in claude_args)])
    shell_cmd = f"cd {shlq(cwd)} 2>/dev/null || cd; {claude}; exec $SHELL"
    run_cmd = ["bash", "-lc", shell_cmd]

    candidates = []

    # 1. $TERMINAL — the cwd is set via the spawned process's working directory.
    term_env = host_env("TERMINAL")
    if term_env and host_has(term_env):
        candidates.append((term_env, [term_env, "-e", *run_cmd]))

    # 2. The freedesktop standard launcher for the configured default terminal.
    if host_has("xdg-terminal-exec"):
        candidates.append(("xdg-terminal-exec",
                           ["xdg-terminal-exec", *run_cmd]))

    # 3. Known terminals, in preference order.
    for name, builder in KNOWN_TERMINALS:
        if host_has(name):
            candidates.append((name, [*builder(cwd), *run_cmd]))

    # flatpak-spawn sets the host cwd via the spawned process, so pass cwd only
    # when running unsandboxed (Popen's cwd has no effect through the portal).
    popen_cwd = None if in_flatpak() else cwd
    for name, argv in candidates:
        try:
            on_host(argv, cwd=popen_cwd)
            return True, name
        except OSError:
            continue
    return False, None


def open_resume_terminal(session):
    """Resume an existing session in a terminal."""
    # Don't validate the session's cwd with os.path.isdir() when sandboxed: the
    # Flatpak only mounts ~/.claude, so project dirs read as missing and we'd
    # fall back to $HOME — where `claude --resume` can't find the session. The
    # dir exists on the host, so trust it and let the host shell fall back if
    # it's genuinely gone.
    cwd = session.cwd
    if not in_flatpak() and not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    return open_terminal(cwd, ["--resume", session.session_id])


def open_new_terminal(cwd, name):
    """Start a fresh session in `cwd`, optionally with a display name.

    `--name` is Claude Code's own flag for this, so the name shows up in the
    prompt box and the /resume picker, and lands in the session file as the same
    custom title `/rename` writes — which is what this app reads back.
    """
    return open_terminal(cwd, ["--name", name] if name else [])


def shlq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class SessionRow(Adw.ActionRow):
    def __init__(self, session, stars, on_resume, on_rename, on_delete,
                 on_star_changed):
        super().__init__()
        self.session = session
        self.stars = stars
        self.on_star_changed = on_star_changed
        self._refresh_title()
        self._refresh_subtitle()
        self.set_title_lines(1)
        self.set_subtitle_lines(1)
        self.set_activatable(True)

        # Star toggle (prefix).
        self.star = Gtk.ToggleButton(valign=Gtk.Align.CENTER)
        self.star.add_css_class("flat")
        self.star.set_active(stars.is_starred(session.session_id))
        self._refresh_star_icon()
        self.star.connect("toggled", self._on_star_toggled)
        self.add_prefix(self.star)

        # Resume (suffix). Reference self.session so it stays correct if the
        # row's session is updated in place after a file change.
        resume = Gtk.Button(label="Resume", valign=Gtk.Align.CENTER)
        resume.add_css_class("suggested-action")
        resume.connect("clicked", lambda *_: on_resume(self.session))
        self.add_suffix(resume)

        # Overflow menu: Rename…, Delete.
        menu = Gtk.MenuButton(icon_name="view-more-symbolic",
                              valign=Gtk.Align.CENTER)
        menu.add_css_class("flat")
        menu.set_tooltip_text("More actions")
        popover = Gtk.Popover(has_arrow=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for label, cb, extra in (
            ("Rename…", lambda: on_rename(self), None),
            ("Delete", lambda: on_delete(self), "destructive-action"),
        ):
            item = Gtk.Button(label=label)
            item.add_css_class("flat")
            if extra:
                item.add_css_class(extra)
            item.get_child().set_halign(Gtk.Align.START)
            item.connect("clicked",
                         lambda _b, fn=cb: (popover.popdown(), fn()))
            box.append(item)
        popover.set_child(box)
        menu.set_popover(popover)
        self.add_suffix(menu)

        self.connect("activated", lambda *_: on_resume(self.session))

    def _refresh_title(self):
        self.set_title(GLib.markup_escape_text(self.session.title))

    def _refresh_subtitle(self):
        self.set_subtitle(GLib.markup_escape_text(
            f"{self.session.cwd}  ·  {human_age(self.session.mtime)}"))

    def update_session(self, session):
        """Replace the row's data in place after the session file changed."""
        self.session = session
        self._refresh_title()
        self._refresh_subtitle()

    def _refresh_star_icon(self):
        active = self.star.get_active()
        self.star.set_icon_name(
            "starred-symbolic" if active else "non-starred-symbolic")
        self.star.set_tooltip_text("Unstar" if active else "Star")

    def _on_star_toggled(self, _btn):
        self.stars.set(self.session.session_id, self.star.get_active())
        self._refresh_star_icon()
        self.on_star_changed(self)

    def apply_new_title(self, new_title):
        self.session.title = new_title
        self.session.search_blob += " " + new_title.lower()
        self.session._tokens = None      # rebuilt on next use
        self._refresh_title()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Claude Code Sessions")
        self.set_default_size(720, 760)
        self.all_rows = []
        self.rows_by_id = {}
        self.stars = Stars()
        self.index = Index()
        self._scan_lock = threading.Lock()
        self._monitors = []
        self._monitored = set()
        self._rescan_pending = False
        # Where the New session dialog starts from; follows the last one you
        # opened so a second session in the same project is a single click.
        self._last_location = str(Path.home())

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        new = Gtk.Button(icon_name="list-add-symbolic")
        new.set_tooltip_text("New session (Ctrl+N)")
        new.connect("clicked", lambda *_: self.new_session())
        header.pack_start(new)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Rescan sessions")
        refresh.connect("clicked", lambda *_: self.reload())
        header.pack_start(refresh)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search title, directory, or content")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_: self._filter())
        header.set_title_widget(self.search)

        self.toast = Adw.ToastOverlay()
        toolbar.set_content(self.toast)

        self.stack = Gtk.Stack()
        self.toast.set_child(self.stack)

        # Loading / empty state.
        self.status = Adw.StatusPage(
            icon_name="content-loading-symbolic",
            title="Loading sessions…",
        )
        self.stack.add_named(self.status, "status")

        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=900)
        scroller.set_child(clamp)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(12)
        content.set_margin_end(12)
        # Two sections: starred favorites on top, everything else below.
        self.fav_header = self._section_header("Favorites")
        self.fav_listbox = self._make_list()
        self.all_header = self._section_header("All sessions", top=True)
        self.all_listbox = self._make_list()
        for w in (self.fav_header, self.fav_listbox,
                  self.all_header, self.all_listbox):
            content.append(w)
        clamp.set_child(content)
        self.stack.add_named(scroller, "list")

        self.reload()
        self._setup_monitor()

    @staticmethod
    def _section_header(text, top=False):
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("heading")
        label.add_css_class("dim-label")
        label.set_margin_start(4)
        label.set_margin_top(12 if top else 0)
        label.set_margin_bottom(2)
        return label

    def _make_list(self):
        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        lb.set_valign(Gtk.Align.START)
        lb.set_sort_func(self._sort_by_recent)
        return lb

    def reload(self):
        # Only show the loading screen on the very first scan; later scans
        # (manual refresh or file-change driven) diff in place without flashing.
        if not self.all_rows:
            self.stack.set_visible_child_name("status")
            self.status.set_icon_name("content-loading-symbolic")
            self.status.set_title("Loading sessions…")
            self.status.set_description(None)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        # One scan at a time: overlapping passes would race on the index.
        with self._scan_lock:
            sessions = scan_sessions(self.index)
        GLib.idle_add(self._apply, sessions)

    def _apply(self, sessions):
        """Reconcile the current rows with a fresh scan, touching only what
        actually changed (add / remove / retitle), preserving scroll + search."""
        new_by_id = {s.session_id: s for s in sessions}

        # Removed sessions.
        for sid in list(self.rows_by_id):
            if sid not in new_by_id:
                row = self.rows_by_id.pop(sid)
                parent = row.get_parent()
                if parent is not None:
                    parent.remove(row)
                if row in self.all_rows:
                    self.all_rows.remove(row)

        # New and changed sessions.
        for sid, s in new_by_id.items():
            row = self.rows_by_id.get(sid)
            if row is None:
                row = SessionRow(s, self.stars, self._resume,
                                 self._confirm_rename, self._confirm_delete,
                                 self._on_star_changed)
                self.rows_by_id[sid] = row
                self.all_rows.append(row)
                self._target_list(row).append(row)
            else:
                old = row.session
                if (old.title, old.mtime, old.cwd) != (s.title, s.mtime, s.cwd):
                    row.update_session(s)

        if not self.all_rows:
            self.status.set_icon_name("dialog-information-symbolic")
            self.status.set_title("No sessions found")
            self.status.set_description(f"Looked in {PROJECTS_DIR}")
            self.stack.set_visible_child_name("status")
            return False

        self.stack.set_visible_child_name("list")
        self.fav_listbox.invalidate_sort()   # re-order for any changed mtimes
        self.all_listbox.invalidate_sort()
        self._filter()                        # apply search + section visibility
        return False

    # -- Live updates: watch ~/.claude/projects and diff on change ----------- #
    def _setup_monitor(self):
        if not PROJECTS_DIR.is_dir():
            return
        self._watch_dir(Gio.File.new_for_path(str(PROJECTS_DIR)))
        try:
            for child in PROJECTS_DIR.iterdir():
                if child.is_dir():
                    self._watch_dir(Gio.File.new_for_path(str(child)))
        except OSError:
            pass

    def _watch_dir(self, gfile):
        path = gfile.get_path()
        if path in self._monitored:
            return
        try:
            monitor = gfile.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            return
        monitor.connect("changed", self._on_dir_changed)
        self._monitors.append(monitor)
        self._monitored.add(path)

    def _on_dir_changed(self, _monitor, gfile, _other, event):
        # A brand-new project directory needs its own watch so we see the
        # session files created inside it.
        if event == Gio.FileMonitorEvent.CREATED:
            try:
                is_dir = (gfile.query_file_type(
                    Gio.FileQueryInfoFlags.NONE, None) == Gio.FileType.DIRECTORY)
            except GLib.Error:
                is_dir = False
            if is_dir:
                self._watch_dir(gfile)
        self._schedule_rescan()

    def _schedule_rescan(self):
        # Coalesce bursts of writes into at most one rescan per ~800ms.
        if self._rescan_pending:
            return
        self._rescan_pending = True
        GLib.timeout_add(800, self._fire_rescan)

    def _fire_rescan(self):
        self._rescan_pending = False
        threading.Thread(target=self._scan_worker, daemon=True).start()
        return False  # one-shot

    def _sort_by_recent(self, a, b):
        return ((b.session.mtime > a.session.mtime)
                - (b.session.mtime < a.session.mtime))

    def _target_list(self, row):
        starred = self.stars.is_starred(row.session.session_id)
        return self.fav_listbox if starred else self.all_listbox

    def _on_star_changed(self, row):
        # Move the row into the section that now matches its starred state.
        target = self._target_list(row)
        parent = row.get_parent()
        if parent is not target:
            if parent is not None:
                parent.remove(row)
            target.append(row)
        self._update_sections()

    def _filter(self):
        text = self.search.get_text()
        if not text.split():
            for row in self.all_rows:
                row.set_visible(True)
        else:
            query = Query(text, [r.session for r in self.all_rows])
            for row in self.all_rows:
                row.set_visible(query.keeps(row.session))
        self._update_sections()

    def _update_sections(self):
        # Show a section only when it has at least one row passing the filter;
        # only label "All sessions" when the Favorites section is also shown.
        fav = any(r.get_visible() and self.stars.is_starred(r.session.session_id)
                  for r in self.all_rows)
        others = any(r.get_visible()
                     and not self.stars.is_starred(r.session.session_id)
                     for r in self.all_rows)
        self.fav_header.set_visible(fav)
        self.fav_listbox.set_visible(fav)
        self.all_header.set_visible(fav and others)
        self.all_listbox.set_visible(others)

    # -- New session --------------------------------------------------------- #
    def new_session(self):
        """Ask for a name and a directory, then start Claude Code there."""
        group = Adw.PreferencesGroup()
        name_row = Adw.EntryRow(title="Name (optional)")
        loc_row = Adw.EntryRow(title="Location", text=self._last_location)
        browse = Gtk.Button(icon_name="folder-open-symbolic",
                            valign=Gtk.Align.CENTER,
                            tooltip_text="Browse…")
        browse.add_css_class("flat")
        loc_row.add_suffix(browse)
        group.add(name_row)
        group.add(loc_row)

        # The name doubles as the folder to create inside the chosen location,
        # kept in sync as you type so the field always shows where you'll land.
        # `base` is the location without that folder; a manual edit of the field
        # wins and stops us rewriting it.
        state = {"base": self._last_location, "editing": False, "touched": False}

        def sync_location(*_):
            if state["touched"]:
                return
            slug = slugify(name_row.get_text())
            state["editing"] = True
            loc_row.set_text(os.path.join(state["base"], slug) if slug
                             else state["base"])
            state["editing"] = False

        def on_location_edited(*_):
            if not state["editing"]:
                state["touched"] = True

        def on_location_picked(path):
            state["base"] = path        # you picked the parent; re-append
            state["touched"] = False
            sync_location()

        name_row.connect("notify::text", sync_location)
        loc_row.connect("notify::text", on_location_edited)
        browse.connect("clicked", lambda *_: self._choose_location(
            loc_row.get_text(), on_location_picked))

        dialog = Adw.AlertDialog(
            heading="New session",
            body="The name becomes a folder inside the location. "
                 "Anything missing is created.",
        )
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("start", "Start")
        dialog.set_response_appearance("start",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("start")
        dialog.set_close_response("cancel")

        def submit(*_):
            # Enter in either field is "Start" (an EntryRow eats the keypress,
            # so the dialog's default response never sees it).
            dialog.close()
            self._start_new_session(name_row.get_text(), loc_row.get_text())

        name_row.connect("entry-activated", submit)
        loc_row.connect("entry-activated", submit)
        dialog.connect("response", self._on_new_session_response,
                       name_row, loc_row)
        dialog.present(self)
        name_row.grab_focus()

    def _choose_location(self, current, on_picked):
        chooser = Gtk.FileDialog(title="Choose a location", modal=True)
        start = os.path.expanduser(current.strip() or "~")
        # Open at the nearest existing ancestor, so browsing still works while a
        # not-yet-created path sits in the field. Ask the host: project dirs
        # aren't visible in here (we mount only ~/.claude).
        while start != "/" and not host_isdir(start):
            start = os.path.dirname(start)
        chooser.set_initial_folder(Gio.File.new_for_path(start))
        chooser.select_folder(self, None, self._on_location_chosen, on_picked)

    def _on_location_chosen(self, chooser, result, on_picked):
        try:
            folder = chooser.select_folder_finish(result)
        except GLib.Error:
            return                      # dismissed
        path = folder.get_path()
        if not path:
            return
        path = host_path(path)
        if _DOC_PORTAL_RE.match(path + "/"):
            # Couldn't be resolved to a real path, so it's no use to the host
            # terminal. Keep whatever's in the field rather than point it at a
            # mount that will vanish.
            print(f"could not resolve portal path: {path}", file=sys.stderr)
            self.toast.add_toast(
                Adw.Toast(title="Couldn't resolve that folder — type the path"))
            return
        on_picked(path)

    def _on_new_session_response(self, _dialog, response, name_row, loc_row):
        if response == "start":
            self._start_new_session(name_row.get_text(), loc_row.get_text())

    def _start_new_session(self, name, location):
        name = name.strip()
        location = location.strip()
        if not location:
            self.toast.add_toast(Adw.Toast(title="Choose a location first"))
            return
        cwd = os.path.normpath(os.path.expanduser(location))
        if not os.path.isabs(cwd):
            self.toast.add_toast(
                Adw.Toast(title=f"“{location}” isn't an absolute path"))
            return

        created = not host_isdir(cwd)
        if created:
            err = host_mkdir(cwd)
            if err:
                self.toast.add_toast(
                    Adw.Toast(title=f"Couldn't create {cwd}: {err}"))
                return

        ok, term = open_new_terminal(cwd, name)
        if not ok:
            self.toast.add_toast(
                Adw.Toast(title="No supported terminal emulator found"))
            return
        # Reopen at the parent when the folder came from the name, so the next
        # new session lands beside this one instead of nested inside it.
        slug = slugify(name)
        self._last_location = (os.path.dirname(cwd)
                               if slug and os.path.basename(cwd) == slug
                               else cwd)
        opened = "Created and started in" if created else "Starting in"
        self.toast.add_toast(Adw.Toast(title=f"{opened} {cwd} ({term})"))

    def _confirm_rename(self, row):
        entry = Gtk.Entry(text=row.session.title, activates_default=True,
                          hexpand=True)
        entry.set_margin_top(6)
        dialog = Adw.AlertDialog(
            heading="Rename session",
            body="Set a custom name (as if you ran /rename in Claude Code).",
        )
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename",
                                       Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_rename_response, row, entry)
        dialog.present(self)

    def _on_rename_response(self, _dialog, response, row, entry):
        if response != "rename":
            return
        new_title = entry.get_text().strip()
        if not new_title or new_title == row.session.title:
            return
        try:
            rename_session(row.session, new_title)
        except OSError as exc:
            self.toast.add_toast(Adw.Toast(title=f"Couldn't rename: {exc}"))
            return
        row.apply_new_title(new_title)
        self.toast.add_toast(Adw.Toast(title="Session renamed"))

    def _confirm_delete(self, row):
        session = row.session
        dialog = Adw.AlertDialog(
            heading="Delete session?",
            body=(f"“{session.title}”\n{session.cwd}\n\n"
                  "The session file will be moved to the trash."),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response, row)
        dialog.present(self)

    def _on_delete_response(self, _dialog, response, row):
        if response != "delete":
            return
        session = row.session
        try:
            if in_flatpak():
                # The sandbox has no trash backend; trash on the host where the
                # file actually lives (and where the trash directory is).
                rc = on_host(["gio", "trash", session.path]).wait()
                if rc != 0:
                    raise GLib.Error(f"gio trash exited with status {rc}")
            else:
                Gio.File.new_for_path(session.path).trash(None)
        except GLib.Error as exc:
            self.toast.add_toast(Adw.Toast(title=f"Couldn't delete: {exc.message}"))
            return
        parent = row.get_parent()
        if parent is not None:
            parent.remove(row)
        if row in self.all_rows:
            self.all_rows.remove(row)
        self.toast.add_toast(Adw.Toast(title="Session moved to trash"))
        if not self.all_rows:
            self.status.set_icon_name("dialog-information-symbolic")
            self.status.set_title("No sessions found")
            self.status.set_description(f"Looked in {PROJECTS_DIR}")
            self.stack.set_visible_child_name("status")
        else:
            self._update_sections()

    def _resume(self, session):
        ok, term = open_resume_terminal(session)
        if ok:
            self.toast.add_toast(
                Adw.Toast(title=f"Resuming in {term} — {session.cwd}")
            )
        else:
            self.toast.add_toast(
                Adw.Toast(title="No supported terminal emulator found")
            )


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_shutdown(self):
        win = self.props.active_window
        if win is not None:
            win.index.save(force=True)     # the throttle doesn't apply on exit
        Adw.Application.do_shutdown(self)

    def do_startup(self):
        Adw.Application.do_startup(self)
        action = Gio.SimpleAction.new("new-session", None)
        action.connect("activate", self._on_new_session_action)
        self.add_action(action)
        self.set_accels_for_action("app.new-session", ["<Primary>n"])

    def _on_new_session_action(self, *_):
        win = self.props.active_window
        if win is not None:
            win.new_session()

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()
        win.search.grab_focus()


if __name__ == "__main__":
    App().run(None)
