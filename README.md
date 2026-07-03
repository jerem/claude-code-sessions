# Claude Code Sessions

A small libadwaita/GTK4 dashboard that lists every Claude Code session found
under `~/.claude/projects`, newest first, showing each session's title and
working directory — and lets you resume any of them in a terminal with one
click.

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
  entry when you rename a session. No access outside `~/.claude` is granted.
- `--talk-name=org.freedesktop.Flatpak` — the only way to reach the host. The
  app shells out via `flatpak-spawn --host` to launch your terminal, run
  `claude --resume`, and `gio trash` deleted sessions — none of which exist
  inside the sandbox.

It never needs broad home access: resumed terminals open as host processes, so
they already see your real files. Starred sessions are stored in the app's own
config directory, not in `~/.claude`.

## How it works

- **Discovery** — scans `~/.claude/projects/*/*.jsonl`. Each `.jsonl` is one
  session. For each, it reads the working directory (`cwd`), the AI-generated
  title (falling back to the first user prompt), and the file's modification
  time, then sorts newest first.
- **Search** — the box matches every whitespace-separated term (AND) against
  the title, working directory, session id, *and the conversation content*, so
  you can find a session by something you typed in it.
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
  scroll position and search text are preserved. The manual refresh button
  forces an immediate re-scan.

### Which terminal opens

It honours your system default, in this order:

1. `$TERMINAL` environment variable
2. `xdg-terminal-exec` (the freedesktop default-terminal standard)
3. the first installed terminal it recognises — ghostty, ptyxis, gnome-console
   (kgx), gnome-terminal, konsole, kitty, alacritty, foot, wezterm, xterm

Set `TERMINAL=ghostty` (or whatever you use) to force a specific one.
