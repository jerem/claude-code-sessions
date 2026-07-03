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
# Our own state (starred sessions). Maps to ~/.config outside the sandbox and to
# ~/.var/app/<id>/config inside it — writable in both cases.
STAR_PATH = (Path(GLib.get_user_config_dir())
             / "claude-code-sessions" / "starred.json")


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
    title = None          # AI-generated title ("ai-title")
    custom_title = None   # user-set name via /rename ("custom-title")
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
                if '"customTitle"' in line:
                    try:
                        custom_title = (json.loads(line).get("customTitle")
                                        or custom_title)
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

    # A user's /rename (custom-title) wins over the auto-generated ai-title.
    display_title = custom_title or title or first_prompt or "(untitled session)"
    if len(display_title) > 90:
        display_title = display_title[:89] + "…"

    blob = " ".join([custom_title or "", title or "", cwd, path.stem,
                     " ".join(prompts)]).lower()

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
        self._refresh_title()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Claude Code Sessions")
        self.set_default_size(720, 760)
        self.all_rows = []
        self.rows_by_id = {}
        self.stars = Stars()
        self._monitors = []
        self._monitored = set()
        self._rescan_pending = False

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
        sessions = scan_sessions()
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
        terms = self.search.get_text().lower().split()
        for row in self.all_rows:
            row.set_visible(not terms or row.session.matches(terms))
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

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()
        win.search.grab_focus()


if __name__ == "__main__":
    App().run(None)
