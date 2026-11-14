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

Sidebar keys: `j`/`k` move, `enter` jump to the project, `l` expand, `g` message Gopher about it,
`i` message its agent (both in a popup that closes when you send), `m`/`M` file an inbox item, `x`
mark finished work seen, `r` one-line Haiku summary, `P` cycle Gopher's proactivity, `q` quit.
For the pinned project: `b` inbox, `n` new inbox item, `u` refresh STATUS, `e` edit
`human/STATUS.md`, plus any custom actions from the root `config.toml`. Each one is a header
button and a hotkey that runs a shell command for the pinned project and shows its output:

```toml
[[tui.actions]]
key = "s"
label = "Status"
command = "git status --short"
cwd = "checkout"   # "checkout" (the project's first checkout, default) or "project"
```

The command gets `GOPHER_ROOT`, `GOPHER_PROJECT`, and `GOPHER_PROJECT_DIR` in its environment. A
key that clashes with a built-in key is skipped with a warning.

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
