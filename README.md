# claude-gopher

A project system and secretary agent for running many Claude Code sessions at once: `gopher`
helps you `workon` your separate projects.

- **Projects**: each body of work has a directory (status, plans, notes, resources, inbox) and one
  or more linked git checkouts.
- **Gopher**: a Claude Code session that lives in tmux window 0 (and a `prefix g` popup). Tell it
  anything about any project; it files it in that project's inbox or messages the project's agent.
- **TUI**: a live list of project blocks: their agents' states, summaries, and PRs.

Design: [docs/design.md](docs/design.md).

## Install

```bash
uv tool install --editable .
```

## Session state hook

`gopher hook state` records each Claude Code session's state (`idle`, `working`, `waiting`) in
`<state_dir>/sessions/<session_id>.json`. Register it for these events in `~/.claude/settings.json`
(see `gopher hook state --print-settings`): SessionStart, UserPromptSubmit, PreToolUse,
PostToolUse, PermissionRequest, Notification, Stop, SessionEnd.

Live sessions are also read from Claude Code's own registry (`~/.claude/sessions/<pid>.json`,
undocumented), so sessions that started before the hook was installed still show up. Hook records
add the project and win when newer. If the registry is missing, gopher falls back to hook records.

```bash
gopher agents              # running agents (WINDOW = window.pane name)
gopher list                # project names and summaries (--names for scripts)
gopher projects            # overview: state, agents, unread inbox (! = wake), checkouts, summary
```

## Roots and projects

A deployment lives in a root directory, meant to be its own git repo: `config.toml`, `prompts/`,
`projects/`, `gopher/`, and a gitignored `tmp/`. Roots are named in
`$XDG_CONFIG_HOME/claude-gopher/config.toml`; pick one with `--root` or `$GOPHER_HOME`.

```bash
gopher setup                            # create or adopt a root, install hooks, interview to configure
gopher setup --review                   # re-run the interview on an existing root
gopher new ~/workon                     # scriptable: create, git init, register (or --from <git-url>)
gopher project new my-feature --checkout ~/src/repo-my-feature
gopher project rename my-feature better-name
gopher checkout move ~/src/repo-my-feature other-project
gopher checkout list
```

Checkouts are never written to; ownership lives in `projects/<p>/checkouts.toml`.

## Gopher and the TUI

```bash
gopher launch                           # start the gopher tmux session; link its window at index 0
gopher tui                              # the dashboard (gopher launch runs it in the left pane)
```

Source `examples/tmux.conf` from your tmux config for the `prefix g` popup and to run
`gopher launch` whenever a client attaches. Gopher runs `claude --model sonnet` by default; set
`[gopher] model` in the root's `config.toml` to change it.

`gopher workon term` lays a project window out as the sidebar (left) and the agent;
`gopher workon sidebar --all` adds the sidebar to open windows.

Sidebar keys: `j`/`k` move, `enter` jump to the project, `o` open its window (restoring the last
snapshot) or jump to it, `c` close its window (after a y/n), `+`/`-` raise or lower its priority, `/` filter the list
(letters in order, or a glob; enter keeps the filter, escape clears it), `l` expand, `g` message Gopher about it,
`i` message its agent (both in a popup that closes when you send), `m`/`M` file an inbox item, `x`
mark finished work seen, `r` one-line Haiku summary, `P` cycle Gopher's proactivity, `q` quit.
`k` at the top of the list moves into the header buttons; there `h`/`l` move between them and
`j` returns to the list (`k` stays put); a focused button shows a one-line hint below the row. For the pinned project: `b` inbox, `n` new inbox item, `u` refresh STATUS, `e` edit
`human/STATUS.md`, `Q` close this tab (after a y/n; `o` restores it), plus any custom actions from the root `config.toml`. Each one is a header
button and a hotkey that runs a shell command for the pinned project in a tmux popup:

```toml
[[tui.actions]]
key = "s"
label = "Status"
command = "git status --short"
cwd = "checkout"   # "checkout" (the project's first checkout, default) or "project"
match = "~/src/*"   # optional: only for projects with a checkout matching this glob
description = "Show uncommitted changes"   # optional: shown when the button is focused
```

Other projects' blocks show the summary cut to `[tui] summary_lines` rows (default 2; 0 hides it).
After `[tui] idle_seconds` (default 60) without input, a sidebar returns to its header and the
top of the list. For gopher development, `[dev] reload = true` (with `uv tool install -e
'.[dev]'`) makes sidebars restart when you run `gopher dev reload`, which first checks the TUI
starts.
An agent's "done" counts as seen once its pane has had focus for `[tui] seen_dwell_seconds`
(default 5).

With `match`, the button shows only when the project has a matching checkout, and the command
runs in the first one that matches. The command gets `GOPHER_ROOT`, `GOPHER_PROJECT`, and `GOPHER_PROJECT_DIR` in its environment. A
key that clashes with a built-in key is skipped with a warning.

## Archive, close, reopen

`gopher archive|close|reopen <project>` print the plan, then ask you to type `yes` (agents pass
`--yes` only after you approve the plan; `--dry-run` only prints it). Archive moves the project to
`archived/` and runs `[lifecycle] on_archive` in its checkouts. Close runs `on_close`, removes the
project's git worktrees (`git worktree remove`, no force; then `git branch -d`), runs `on_remove`,
and moves it to `closed/`; plain clones, `[checkouts] protected` paths, and checkouts another
project owns are kept. Uncommitted changes or commits on no remote block a close. An open project
window is snapshotted and closed first. Lifecycle commands get `GOPHER_PROJECT`,
`GOPHER_CHECKOUT`, and `GOPHER_ROOT`. In the sidebar: `A` archive and `X` close the window's own
project (header buttons with hints). Every TUI confirmation wants `yes` typed.

## Saving the root

`gopher save` commits the root's changes, one commit per project (`alpha: update STATUS, plan`)
plus one for the root's own files, then pushes to the branch's upstream (`--no-push` to skip,
`--dry-run` to preview). It refuses while a merge or rebase is in progress. Gopher runs it every
`[gopher] save_every_hours` (default 2; 0 turns the timer off) via a session-scoped CronCreate
job, at the end-of-day wrap, and on request; project agents commit there only when asked.

## Priority

`gopher project priority <project> [0-3|+|-]` shows or sets a project's priority, stored as
`priority:` in its `human/STATUS.md` frontmatter. Starred projects get their own section at the
top of the TUI, most stars first, with the usual colour order inside each level.

## Project windows

`gopher workon close <project>` triggers a tmux-resurrect save, keeps that window's part of it
(layout, pane directories, which pane is the sidebar, the Claude session id recorded by
tmux-assistant-resurrect) in `tmp/gopher/windows/<project>.json`, then closes the window.
`gopher workon open <project>` rebuilds the window from that snapshot, back at its old index when
free, and resumes the agent with `--resume`; with no snapshot it opens the standard sidebar and
agent layout. `gopher workon snapshot <project>` saves without closing. Needs tmux-resurrect
(and tmux-assistant-resurrect for the session ids).

## Prompts

Core instructions for each context (`all`, `project`, `gopher`, `resources`, `archive`, `close`,
`reopen`, `inbox`, `tui-summary`) ship with gopher; a root adds site text in
`prompts/<context>.md`. `gopher prompt <context>` prints the result. `gopher hook context`
(installed by `gopher setup`) injects `all` + `project` into project sessions at SessionStart.

## workon

```bash
eval "$(gopher shell-init zsh)"          # in ~/.zshrc: gopher function + completion
alias workon='gopher workon'             # optional
workon term my-feature                   # tmux: STATUS editor, shell, agent
workon agent my-feature -- --model opus  # agent here, with the session kickoff
workon source my-feature                 # cd + activate the checkout
```

Agents, per-checkout setup commands, and pre-launch commands live in the root's `config.toml`.

## Inbox

```bash
gopher inbox add my-feature "rebase onto main when CI is green"     # picked up on the next prompt
gopher inbox add my-feature --wake "stop: the API changed"          # delivered when the agent stops
gopher inbox list
```

`gopher hook inbox` (UserPromptSubmit, Stop) delivers items; `gopher setup` installs it.

## Development

```bash
uv run pytest
```
