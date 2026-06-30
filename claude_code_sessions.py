#!/usr/bin/env python3
"""Claude Code Sessions — a small libadwaita/GTK4 dashboard.

Lists every Claude Code session found under ~/.claude/projects (newest first),
shows its working directory, and lets you resume it in a terminal.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"
APP_ID = "io.github.jerem.ClaudeCodeSessions"


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
class Session:
    __slots__ = ("session_id", "cwd", "title", "mtime", "path", "search_blob")

    def __init__(self, session_id, cwd, title, mtime, path, search_blob):
        self.session_id = session_id
        self.cwd = cwd
        self.title = title
        self.mtime = mtime
        self.path = path
        # Lowercased haystack: title + dir + id + the user-side conversation.
        self.search_blob = search_blob

    def matches(self, terms):
        """All whitespace-separated terms must appear somewhere (AND, any field)."""
        return all(t in self.search_blob for t in terms)


def _first_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
    return None


def parse_session(path):
    """Pull just the fields we need out of a session .jsonl, cheaply.

    We substring-test each line before json-parsing it, so we never decode the
    big assistant payloads — only the handful of lines that carry metadata.
    """
    cwd = None
    title = None
    first_prompt = None
    prompts = []          # the user-side text, for content search
    prompt_chars = 0
    PROMPT_CAP = 200_000  # index the whole conversation; just bound runaway logs
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                if cwd is None and '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd")
                    except json.JSONDecodeError:
                        pass
                if '"aiTitle"' in line:
                    try:
                        title = json.loads(line).get("aiTitle") or title
                    except json.JSONDecodeError:
                        pass
                if (prompt_chars < PROMPT_CAP
                        and '"type":"user"' in line.replace(" ", "")):
                    try:
                        msg = json.loads(line).get("message", {})
                        txt = _first_text(msg.get("content"))
                    except json.JSONDecodeError:
                        txt = None
                    # Skip tool results / injected context (they start with '<').
                    if txt and not txt.lstrip().startswith("<"):
                        txt = txt.strip()
                        if first_prompt is None:
                            first_prompt = txt
                        prompts.append(txt)
                        prompt_chars += len(txt)
    except OSError:
        return None

    if cwd is None:
        # Fall back to decoding the directory name (slashes were turned to '-').
        cwd = "/" + path.parent.name.lstrip("-").replace("-", "/")

    display_title = title or first_prompt or "(untitled session)"
    if len(display_title) > 90:
        display_title = display_title[:89] + "…"

    blob = " ".join([title or "", cwd, path.stem, " ".join(prompts)]).lower()

    return Session(
        session_id=path.stem,
        cwd=cwd,
        title=display_title,
        mtime=path.stat().st_mtime,
        path=str(path),
        search_blob=blob,
    )


def scan_sessions():
    sessions = []
    if not PROJECTS_DIR.is_dir():
        return sessions
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        s = parse_session(jsonl)
        if s is not None:
            sessions.append(s)
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


def open_resume_terminal(session):
    """Open the user's default terminal in the session's cwd, resuming it.

    Resolution order:
      1. $TERMINAL              (explicit user override)
      2. xdg-terminal-exec      (the freedesktop default-terminal standard)
      3. first installed terminal we know how to drive (ghostty, ptyxis, …)
    """
    # The session's working directory. Don't validate it with os.path.isdir()
    # when sandboxed: the Flatpak only mounts ~/.claude, so project dirs read as
    # missing and we'd fall back to $HOME — where `claude --resume` can't find
    # the session. The dir exists on the host, so trust it and let the host
    # shell fall back if it's genuinely gone.
    cwd = session.cwd
    if not in_flatpak() and not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    # Login shell so PATH/nvm/etc. are set up. cd explicitly (a flatpak-spawned
    # host process starts in $HOME, not our cwd), then drop to an interactive
    # shell once Claude exits so the window stays open.
    resume = f"{shlq(claude_bin())} --resume {shlq(session.session_id)}"
    shell_cmd = f"cd {shlq(cwd)} 2>/dev/null || cd; {resume}; exec $SHELL"
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


def shlq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class SessionRow(Adw.ActionRow):
    def __init__(self, session, on_resume, on_delete):
        super().__init__()
        self.session = session
        self.set_title(GLib.markup_escape_text(session.title))
        self.set_subtitle(
            GLib.markup_escape_text(
                f"{session.cwd}  ·  {human_age(session.mtime)}"
            )
        )
        self.set_title_lines(1)
        self.set_subtitle_lines(1)
        self.set_activatable(True)

        folder = Gtk.Image.new_from_icon_name("folder-symbolic")
        folder.add_css_class("dim-label")
        self.add_prefix(folder)

        btn = Gtk.Button(label="Resume")
        btn.set_valign(Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda *_: on_resume(session))
        self.add_suffix(btn)

        delete = Gtk.Button(icon_name="user-trash-symbolic")
        delete.set_valign(Gtk.Align.CENTER)
        delete.set_tooltip_text("Delete this session")
        delete.add_css_class("flat")
        delete.connect("clicked", lambda *_: on_delete(self))
        self.add_suffix(delete)

        self.connect("activated", lambda *_: on_resume(session))


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Claude Code Sessions")
        self.set_default_size(720, 760)
        self.all_rows = []

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

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
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_top(18)
        self.listbox.set_margin_bottom(18)
        self.listbox.set_margin_start(12)
        self.listbox.set_margin_end(12)
        self.listbox.set_valign(Gtk.Align.START)
        clamp.set_child(self.listbox)
        self.stack.add_named(scroller, "list")

        self.reload()

    def reload(self):
        self.stack.set_visible_child_name("status")
        self.status.set_icon_name("content-loading-symbolic")
        self.status.set_title("Loading sessions…")
        self.status.set_description(None)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        sessions = scan_sessions()
        GLib.idle_add(self._populate, sessions)

    def _populate(self, sessions):
        self.listbox.remove_all()
        self.all_rows = []

        if not sessions:
            self.status.set_icon_name("dialog-information-symbolic")
            self.status.set_title("No sessions found")
            self.status.set_description(f"Looked in {PROJECTS_DIR}")
            self.stack.set_visible_child_name("status")
            return

        for s in sessions:
            row = SessionRow(s, self._resume, self._confirm_delete)
            self.listbox.append(row)
            self.all_rows.append(row)

        self.stack.set_visible_child_name("list")
        self._filter()
        return False

    def _filter(self):
        terms = self.search.get_text().lower().split()
        for row in self.all_rows:
            row.set_visible(not terms or row.session.matches(terms))

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
                # The sandbox has read-only access to the files and no trash
                # backend; trash on the host where the file actually lives.
                rc = on_host(["gio", "trash", session.path]).wait()
                if rc != 0:
                    raise GLib.Error(f"gio trash exited with status {rc}")
            else:
                Gio.File.new_for_path(session.path).trash(None)
        except GLib.Error as exc:
            self.toast.add_toast(Adw.Toast(title=f"Couldn't delete: {exc.message}"))
            return
        self.listbox.remove(row)
        if row in self.all_rows:
            self.all_rows.remove(row)
        self.toast.add_toast(Adw.Toast(title="Session moved to trash"))
        if not self.all_rows:
            self.status.set_icon_name("dialog-information-symbolic")
            self.status.set_title("No sessions found")
            self.status.set_description(f"Looked in {PROJECTS_DIR}")
            self.stack.set_visible_child_name("status")

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

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


if __name__ == "__main__":
    App().run(None)
