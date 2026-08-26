# Claude Code Sessions

A small libadwaita/GTK4 dashboard that lists every Claude Code session found
under `~/.claude/projects`, newest first, showing each session's title and
working directory — and lets you resume any of them in a terminal with one
click, or start a new named session in any directory.

![GTK4 / libadwaita]

## Run

```bash
python3 claude_code_sessions.py
```

Requires PyGObject with GTK 4 and libadwaita (`python3-gobject`, `gtk4`,
`libadwaita` — already present on most GNOME systems).

To install it as a launchable app, put the script on your `PATH` and copy the
desktop file:

```bash
install -Dm755 claude_code_sessions.py ~/.local/bin/claude-code-sessions
cp io.github.jerem.ClaudeCodeSessions.desktop ~/.local/share/applications/
```

## Flatpak

Build and install with `flatpak-builder` (needs `org.gnome.Sdk//50`):

```bash
flatpak-builder --user --install --force-clean build-dir \
    io.github.jerem.ClaudeCodeSessions.yaml
flatpak run io.github.jerem.ClaudeCodeSessions
```

The sandbox is intentionally narrow:

- `--filesystem=~/.claude` — read the session logs, and append a `custom-title`
  entry when you rename a session. No access outside `~/.claude` is granted, not
  even read-only.
- `--talk-name=org.freedesktop.Flatpak` — the only way to reach the host. The
  app shells out via `flatpak-spawn --host` to launch your terminal, run
  `claude`, `mkdir -p` a new session's location, and `gio trash` deleted
  sessions — none of which exist inside the sandbox.

It never needs broad home access: terminals open as host processes, so they
already see your real files, and a new session's directory is created out there
too. Starred sessions are stored in the app's own config directory, not in
`~/.claude`.

One wrinkle worth knowing about: the file chooser portal doesn't hand a
sandboxed app the folder you picked. It exports it through the Documents portal
and returns a `/run/user/UID/doc/DOCID/name` mount instead — a path that means
nothing to the terminal being launched, and granting `--filesystem` doesn't
change that. The portal's own `Info` method refuses to answer inside the sandbox
("Not allowed in sandbox"), so `host_path()` resolves the id back to a real path
via `flatpak documents` on the host.

## How it works

- **Discovery** — scans `~/.claude/projects/*/*.jsonl`. Each `.jsonl` is one
  session. For each, it reads the working directory (`cwd`), the AI-generated
  title (falling back to the first user prompt), and the file's modification
  time, then sorts newest first. Lines are read as bytes and substring-tested
  before being parsed, so the big assistant payloads are never decoded.
- **The index** — logs get large (a long session can reach hundreds of
  megabytes), and re-reading them all on every scan does not scale. Parsed state
  is cached per file in `$XDG_CACHE_HOME/claude-code-sessions/index.json`,
  including the byte offset parsing stopped at. Session logs are append-only, so
  an unchanged file costs a `stat()` and a grown one costs only its new bytes —
  a file that gained 2 KB is 2 KB of reading, not 350 MB. A file that shrank was
  rewritten rather than appended to, so it is parsed again from the top. Only
  the first scan pays full price; delete the file to force one.
- **Search** — the box matches every whitespace-separated term (AND) against
  the title, working directory, session id, *and the conversation content*, so
  you can find a session by something you typed in it. A term that matches
  nothing verbatim is treated as a typo: it's corrected against the words the
  sessions actually contain (Damerau-Levenshtein — one edit for terms under 8
  characters, two beyond, none under 4, and a swapped pair of letters counts as
  one), then searched as if spelled correctly. So `sesion` and `cluade` find
  what `session` and `claude` find. Correctly spelled queries are unaffected —
  the typo pass only ever runs for a term that was about to match nothing.
- **New session** — the *+* button (or <kbd>Ctrl</kbd>+<kbd>N</kbd>) asks for a
  name and a location, then runs `claude --name <name>` there. The name doubles
  as the folder to create: typing *“Partner Dashboard”* with `~/dev` as the
  location gives `~/dev/partner-dashboard`, kept in sync in the field as you
  type, so you always see where you'll land. Editing the location by hand stops
  that and leaves whatever you typed alone; browsing for a folder sets a new
  parent and re-appends. Anything missing is created (with parents), so a project
  and its first session start in one step, and the dialog reopens at the parent
  so the next session lands beside the last rather than inside it. The name is
  Claude Code's own `--name`, so it shows up in the prompt box and the `/resume`
  picker too.
- **Resume** — clicking *Resume* (or a row) opens your default terminal in the
  session's working directory and runs `claude --resume <session-id>`. The
  shell stays open after Claude exits.
- **Star** — the star toggle moves a session into a **Favorites** section shown
  above the rest (each section ordered most-recent first). Stars persist in the
  app's config directory.
- **Rename** — *⋮ → Rename…* sets a custom name by appending the same
  `custom-title` entry that `/rename` writes, so the name also shows up inside
  Claude Code.
- **Delete** — *⋮ → Delete* asks for confirmation, then moves the session's
  `.jsonl` file to the trash (recoverable from your file manager).
- **Live updates** — a `Gio.FileMonitor` on `~/.claude/projects` watches for
  changes (debounced ~800 ms). New sessions, renames, deletions, and activity
  bumps are reconciled *in place* — only the affected rows change, so your
  scroll position and search text are preserved. This fires on every write to
  the session you are sitting in, which is what makes the index matter: a
  rescan re-reads only what actually changed. The manual refresh button forces
  an immediate re-scan.

### Which terminal opens

It honours your system default, in this order:

1. `$TERMINAL` environment variable
2. `xdg-terminal-exec` (the freedesktop default-terminal standard)
3. the first installed terminal it recognises — ghostty, ptyxis, gnome-console
   (kgx), gnome-terminal, konsole, kitty, alacritty, foot, wezterm, xterm

Set `TERMINAL=ghostty` (or whatever you use) to force a specific one.
