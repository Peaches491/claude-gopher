# claude-gopher: design

This is the design of what is built, and why. The README is the user guide; this file is for
contributors and for agents that need the model behind the commands.

## What it is

gopher runs many Claude Code sessions as **projects**. Each project has a home directory in a
versioned **root**, the git checkouts it owns, and its own agent in a tmux window with a live
**sidebar**. **Gopher**, a secretary agent one keypress away, takes anything the user says about
any project and routes it: an inbox note, a live message to the project's agent, a ticket. It
keeps a journal, answers status questions, maintains the user's own status notes on their
behalf, and runs the root's recurring work on a heartbeat.

Gopher does no project work. Project agents do the work; scripts do anything destructive.

## Decisions

| Topic | Decision |
|---|---|
| Unit of work | A project, not a session: its directory persists across sessions and agents |
| Placement | Gopher's window is owned by a `gopher` tmux session, linked into the main session at index 0, and also reachable as a `prefix g` popup |
| Layout | Each project window: the sidebar (20%, left) and the project's agent (80%) |
| Delivery | Inbox by default; wake a live agent only when the user says so ("tell X now") |
| Busy target | File an inbox item with `wake: true`; the Stop hook hands it to the agent when it goes idle |
| Pickup | Email model: hooks show unread items at session start and before each prompt; the agent summarizes and the user decides |
| Routing | Silent when confident; a menu of candidate projects when not |
| human/STATUS.md | The user's own notes, authoritative; only Gopher may write there, on the user's behalf |
| Summaries | `STATUS.md` frontmatter, kept current by each agent; the TUI can add a cached Haiku line |
| Agent messaging | Native cross-session messaging (`ListAgents` / `SendMessage`); scripts use tmux instead |
| Resources | `resources/<kind>/<name>.md`, one file per external thing, maintained by agents and Gopher |
| Lifecycle | active -> archived (checkouts kept, caches cleared) -> closed (worktrees removed); nothing moves without a yes |
| Recurring work | Data in the root's `schedule/`, run by one heartbeat, never only prompt text |
| Site specifics | In the root's `config.toml` and `prompts/`; nothing in this repo names a company, repo layout, or tool |
| Permissions | Gopher and project agents run in the same permission mode (`auto` by default), so messages flow without approval |

## Platform facts this relies on

- Cross-session messaging (`ListAgents` / `SendMessage`) is on by default and local only. An idle
  receiver starts a new turn; a busy one reads between tool calls. Messages are held for
  approval when sender and receiver run in different permission modes. Only the model can send:
  scripts (the TUI) cannot, and the socket format is undocumented.
- No API exposes an interactive session's working/waiting/idle state, so hooks record it.
- Hooks can inject context (`additionalContext`, 10k character cap) at SessionStart and
  UserPromptSubmit, and a Stop hook can return `{"decision": "block", "reason": ...}` to keep the
  agent going: that is how "wake when idle" works without polling.
- Claude Code cron jobs (CronCreate) fire only while the session is idle, and recurring ones
  expire after 7 days; session-only jobs die with the process.
- Claude Code keeps an undocumented per-process registry (`~/.claude/sessions/<pid>.json`) with
  each session's id, cwd, tmux pane, name, and status.

## The root

A root (`$GOPHER_HOME`, default `~/workon`) is a plain directory that is its own git repository,
separate from this source repo and backed up by pushing it:

```
$GOPHER_HOME
  config.toml       settings; [paths] are relative to the root
  prompts/          site prompts, one file per context (see Prompts)
  schedule/         Gopher's recurring tasks, one markdown file each, plus README.md
  projects/<p>/     open projects (layout below)
  archived/<p>/     archived projects
  closed/<p>/       closed projects
  gopher/           Gopher's own directory: journal/, pending/, STATUS.md, notebook/, changelog.md
  reference/        shared reference library: one file per item, README.md index; Gopher curates it
  tmp/              regenerable and gitignored: tmp/<p>/ scratch, tmp/gopher/ state dir
```

A project directory holds `STATUS.md` (frontmatter + the agent's account), `human/` (the user's
notes), `plan/`, `notes/`, `resources/`, `inbox/` (with `read/`), `prompt/`, `script/`, `data/`,
and `checkouts.toml`. The `all` prompt defines what goes where.

### Roots and discovery

A per-machine, unversioned registry (`$XDG_CONFIG_HOME/claude-gopher/config.toml`) names roots:

```toml
default = "work"
[roots]
work     = "~/workon"
personal = "~/gopher-personal"
```

The root is chosen by `--root` (name or path), then `$GOPHER_HOME`, then the registered root
containing the cwd, then the registry default, then `~/workon`. `gopher workon` and
`gopher launch` export `GOPHER_HOME` for the agents they start. Each root has its own state dir,
so every command shows one deployment.

### Creating and setting up a root

`gopher setup [DIR]` is the front door: it creates the root (or clones one with `--from`, or
repairs an existing one), registers it, offers to make it the default, installs the Claude Code
hooks (showing the entries first and backing up `~/.claude/settings.json`), and offers the setup
interview. `gopher new [DIR]` is the scriptable primitive: create (or `--from` clone) and
register, nothing else.

Creating a root copies the packaged skeleton (`src/claude_gopher/skeleton/`), writes the default
schedule, `git init`s, and commits. In a terminal both commands first show the default schedule
and ask whether to keep, retime, or drop each task, and which to add (`--default-schedule` skips
the questions).

The setup interview launches `claude` in the root with the core `setup` prompt and the package
directory added, so it can read the core prompts and the skeleton. It interviews the user about
repositories, build and validation, the ticket tracker, code review, credentials, lifecycle
cleanup, the schedule, where shared knowledge lives (for example chat and documents, and the
tools that read them), and anything else. It writes `prompts/<context>.md`, `config.toml`, and
`schedule/` entries, shows the diff, and commits on yes. `gopher setup --review` reruns it on an
existing root; `gopher setup --remove-hooks` undoes the hook install.

### Projects and checkouts

`gopher project new <name>` creates a project; `gopher project rename <old> <new>` moves the
directory and carries its state (inbox tracker, session records, window names) over;
`gopher project priority <p> <0-3|+|->` sets stars in `human/STATUS.md` frontmatter.

Checkouts are leaves: nothing is ever written into them. Ownership lives only in the owning
project's `checkouts.toml`:

```toml
[[checkout]]
path = "~/src/monorepo-feature-x"
```

`gopher checkout add|remove|move|list` edit these files. A session's project comes from
`$GOPHER_PROJECT`, else a cwd inside `projects/<p>/`, else the longest owned checkout path
containing the cwd.

## Configuration

Everything specific to one deployment lives in `<root>/config.toml` and `<root>/prompts/`. Every
key below is read by the code; values are illustrative unless marked default.

```toml
[paths]                         # defaults shown
projects_dir  = "projects"
archived_dir  = "archived"
closed_dir    = "closed"
agent_dir     = "gopher"
state_dir     = "tmp/gopher"
prompts_dir   = "prompts"
reference_dir = "reference"
schedule_dir  = "schedule"

[claude]
sessions_dir = "~/.claude/sessions"   # default; Claude Code's process registry

[checkouts]
protected = ["~/src/monorepo"]        # never removed by gopher close

[[checkouts.setup]]                   # run on entering a matching checkout (workon agent/source/term)
match = "~/src/monorepo*"
commands = ['eval "$(my-env-tool activate {{name}})"']   # {{path}} {{name}} {{project}} {{root}}

[workon]
default_agent = "claude"
pre_launch = ["my-auth-check"]        # run before the agent starts
default_checkouts = { shared = "~/src/monorepo" }

[agents.cursor]                       # claude is built in; others are declared like this
command = ["cursor-agent"]
resume_flags = ["--resume"]           # passing one of these skips the kickoff prompt

[lifecycle]
on_archive = ["make clean"]           # per checkout, when archiving
on_close   = ["make clean"]           # per checkout, when closing
on_remove  = ["my-tool forget $GOPHER_CHECKOUT"]   # after a worktree is removed

[tui]
summary_lines = 2                     # default; summary rows per project (0 hides)
idle_seconds = 60                     # default; reset a pinned sidebar after this long idle (0 off)
seen_dwell_seconds = 5                # default; focus time before "done" counts as seen

[[tui.actions]]                       # sidebar buttons
key = "s"
label = "Git"
command = "git status -sb"
match = "~/src/*"                     # optional: only for projects with a matching checkout

[gopher]
model = "sonnet"                      # default
proactivity = "quiet"                 # default; quiet | nudge | proactive
save_every_hours = 2                  # default; interval for a new schedule/save.md (0: no save task)
update_check = true                   # default; add schedule/update-check.md to a new schedule
features = []                         # any of "notebook", "changelog"
update_source = "https://github.com/Peaches491/claude-gopher"   # default

[dev]
reload = false                        # default; see Development reload
```

## Prompts

Behaviour is driven by prompts. The package ships a generic core prompt per context
(`src/claude_gopher/prompts/<context>.md`); a root may add `<root>/prompts/<context>.md`, appended
under a `## Site` heading. Assembly is append-only: a site can refine core rules in prose but not
remove them.

| Context | Used by | Core covers |
|---|---|---|
| `all` | every agent | root layout, STATUS.md, `human/`, checkouts, needs-login, gopher commands |
| `project` | project agents | start of session, STATUS upkeep, resources, inbox, lifecycle proposals |
| `gopher` | Gopher | role, routing, replies and fan-outs, tickets/chat/documents, reference library, lifecycle, saving, updates, schedule, rituals, journal |
| `resources` | agents and Gopher, on demand | `resources/` layout, file format, frontmatter |
| `archive`, `close`, `reopen` | agents and Gopher, on demand | what the transition does, the plan-then-yes flow |
| `inbox` | the inbox hooks | how an injected item is framed |
| `tui-summary`, `tui-detail` | the TUI's Haiku calls | one-line and multi-line summary formats |
| `setup` | the setup interview | topics, and which file each answer goes to |

Gopher's prompt also gets one `gopher-proactivity-<level>.md` and one `gopher-feature-<name>.md`
per enabled feature. Placeholders: `{{root}}`, `{{projects_dir}}`, `{{archived_dir}}`,
`{{closed_dir}}`, `{{agent_dir}}`, `{{prompts_dir}}`, `{{reference_dir}}`, `{{schedule_dir}}`,
`{{tmp_dir}}`, `{{project}}`, `{{project_dir}}`, `{{project_tmp}}`, `{{save_every_hours}}`,
`{{update_check}}`. `gopher prompt <context> [--project P]` prints the assembled text.

Delivery: `gopher hook context` (SessionStart) injects `all` plus `project` for a project
session, capped at 10k characters with a pointer to `gopher prompt`. Gopher's prompt is too long
for that, so `gopher launch` writes it to `<state_dir>/gopher-prompt.md` and passes it with
`--append-system-prompt-file`. On-demand contexts are read with `gopher prompt <context>`.

## Components

### Session state (`gopher hook state`)

The hook writes `<state_dir>/sessions/<session_id>.json` (state, event, pane, window, cwd,
project, transcript, updated, finished). Events map to states: SessionStart, Stop and
Notification `idle_prompt` to `idle`; UserPromptSubmit, PreToolUse and PostToolUse to `working`;
PreToolUse `AskUserQuestion`, PermissionRequest and Notification
`permission_prompt|elicitation_dialog|agent_needs_input` to `waiting`. SessionEnd removes the
record. The hook never fails a session: errors go to `<state_dir>/errors.log`.

Claude Code's process registry decides which sessions are live and gives a baseline state;
hook records add the project and win when newer. Without the registry, records whose pane is
gone are pruned.

On top of the state, the TUI shows two attention marks:
- **done**: the agent finished (Stop) and no client has kept its pane focused for
  `[tui] seen_dwell_seconds` since (or the user pressed `x`). Acks: `<state_dir>/tui/acks.json`.
- **needs login**: the agent ran `gopher needs-login` because it is blocked on credentials the
  user must renew (the `all` prompt says when). It marks the session record and clears on the
  user's next prompt or `gopher needs-login --clear`. A window name starting with the ✕ glyph
  counts too, for setups whose own hooks mark windows that way.

### Inbox

- `gopher inbox add <project> [--wake] [--from NAME] [text]` writes
  `inbox/<YYYYMMDD-HHMMSS>-<slug>.md` with `from`, `created`, and `wake` frontmatter;
  `gopher inbox list` shows unread items. Handled items move to `inbox/read/`.
- `gopher hook context` (SessionStart) and `gopher hook inbox` (UserPromptSubmit) inject unread
  items: full text the first time a session sees one, a one-line reminder after. Per-session
  tracking lives in `<state_dir>/inbox/`, reset on `/clear` and compaction. Capped at 8k.
- Email model: at session start the agent reports where the project stands and one line per
  item, then asks; mid-session it names new items in one line at the end of its reply.
- Wake: `gopher hook inbox` (Stop) blocks the stop once per `wake: true` item; the agent hands it
  to a subagent, which does the work and files the item, then reports in a line. If the agent
  was waiting on the user, it only mentions the item.
- `gopher workon agent` starts sessions with a kickoff message asking for that briefing.

### Gopher

- `gopher launch [--session S]` creates the `gopher` tmux session if missing and links its window
  at index 0 of the current (or named) session; it is idempotent and the shipped tmux config runs
  it when a client attaches. `gopher launch --restart [--continue]` respawns Gopher's pane, fresh
  or continuing its conversation. Gopher runs as `claude --model <[gopher] model> --name gopher
  --permission-mode auto` so peers can message it by name.
- Its directory holds `journal/<date>.md` (everything the user said and where it went),
  `pending/` (fan-outs being tracked: who was asked, who answered), `STATUS.md`, and the
  optional `notebook/` and `changelog.md` features.
- Start of session: start the heartbeat, read every project's frontmatter and `human/STATUS.md`
  and today's journal, and brief the user in a few lines, most urgent first.
- Routing, replies, fan-outs, tickets, the reference library, `human/STATUS.md` edits, and the
  end-of-day and weekly rituals follow the `gopher` prompt. Proactivity (`gopher proactivity`
  or the sidebar's `P`) sets how much it says unprompted, and tells the running Gopher.

### Schedule and heartbeat

Gopher only acts when a message arrives, and Claude Code cron jobs are session-only and expire,
so recurring work lives in data:

- `schedule/<task>.md`: frontmatter `every` (`10m`, `2h`, `1d`) or `at` (`HH:MM`, local, daily),
  optional `days` (`weekdays`, `weekends`, `mon,thu`), `run` (a shell command), `until` (last
  date), `enabled`; the body tells Gopher what to do with the result. Defaults: `save` (every
  `save_every_hours`, runs `gopher save`) and `update-check` (00:03, runs
  `gopher update --check`).
- `gopher schedule tick` (under a lock) runs each due task's command in the root, records runs in
  `<state_dir>/schedule.json`, and prints a section per due task: output, then instructions. It
  adds events: the backup newly overdue, and at the proactive level projects whose STATUS has
  not moved in five working days, each reported once. An `at` task first seen after its slot
  waits for the next one; a missed slot runs once. Ended tasks and malformed files are reported
  once. No output means nothing to do. Existing roots get the default schedule on their first
  tick.
- The heartbeat is one session-only CronCreate job every 10 minutes, created at the start of each
  session and after a restart; Gopher runs the tick and stays silent when it prints nothing.
  `gopher launch --exec-agent` records the start, and the tick asks Gopher to recreate the job
  after six days, before the 7-day expiry.
- `gopher schedule` lists tasks with their last and next runs. Monitors and reminders the user
  asks for become tasks with an `until`.

### Saving the root

`gopher save` groups changed paths by project directory, commits each project separately with a
message naming the areas touched, commits the rest as `root: ...`, and pushes. It refuses when
the root is not its own repository or a merge or rebase is in progress. The `save` task runs it;
Gopher also runs it at the end-of-day wrap and on request, and never hand-crafts commits there.

### Sidebar and dashboard (`gopher tui`)

`gopher tui` in a project window pins that project (from the agent in the pane, the cwd, or the
window name; `--project` overrides). Without one it is the full dashboard. It polls every 5
seconds.

- **Pinned header**: name, state, summary and cached Haiku line, each agent's state and name,
  unread inbox count, PR and ticket links, checkouts, and a row of buttons: custom actions
  (`[[tui.actions]]`, run in a tmux popup in the first matching checkout), `b` Inbox, `n` Note,
  `u` Refresh (asks the agent beside it to refresh STATUS.md), `e` Human (edits
  `human/STATUS.md` in a popup), and `.` More.
- **Project list**: every other project as a compact block, under "Priority" (starred) and
  "Projects". The left bar is coloured by urgency: yellow (an agent needs input, or
  `waiting-review`), red (needs login, or `blocked`), green (`active`), grey otherwise, and grey
  for any project without an open window. Within a colour: most urgent agent first, then most
  recent. Resource links hide closed tickets and PRs, show merged PRs for a day, and collapse a
  PR stack to its top PR.
- **Bottom line**: the backup (💾 age since the last save, unsaved changes as `+lines -lines
  ~files`, ⇡ unpushed commits, ✓ when clean) and the heartbeat (💓 age of the last tick, ⏭ the
  next task due). The backup turns yellow when a save is overdue and red without a repository or
  upstream; the heartbeat turns yellow after 30 silent minutes. Git is read once a minute.

| Key | Action |
|---|---|
| `j` / `k`, `home` / `end` | move; top and bottom |
| `enter`, `t` | jump to the project's agent (or its window) |
| `l` | expand: live facts, a multi-line Haiku summary, and STATUS.md (`h`/`esc` back; closes after 90 s idle) |
| `/` | filter by name (letters in order, or a glob); `esc` clears |
| `o` / `c` | open the project's window (restoring a saved layout) / save and close it |
| `g` | popup on Gopher with `[project: X] ` prefilled |
| `i` | popup on the project's agent, prefilled with where it came from (no agent: an inbox note) |
| `m` / `M` | file an inbox item / one that wakes the agent |
| `x` | mark "done" as seen |
| `r` | refresh the project's Haiku line |
| `+` / `-` | raise or lower priority |
| `P` | cycle Gopher's proactivity |
| `.` | More: close the window (or this tab), archive, close the project |
| `Q`, `q` | close this tab; quit |
| `ctrl+r` | refresh now |

In the pinned inbox list: `enter` prefills the agent beside it with the item's path, `l` shows
it, `x` marks it read, `b`/`esc` closes the list. A pinned sidebar returns to its header after
`[tui] idle_seconds` without input.

Popups (`gopher popup gopher|agent`, also `prefix g`) dim the window behind and record the outer
client and panes in `@gopher_popup_*` tmux options. Agent popups attach through a temporary
grouped session so the outer client's window does not move, and a popup opened with prefilled
text closes itself when that agent receives the prompt.

### Windows (`gopher workon`)

`gopher` is the only entry point (`alias workon='gopher workon'` if you like the short name).

- `agent [project [checkout]] [-- agent args]`: cd to the checkout (or the project directory),
  export `GOPHER_HOME` and `GOPHER_PROJECT`, name the tmux window after the project, run matching
  `[[checkouts.setup]]` commands and `[workon] pre_launch`, then start the agent with its kickoff.
- `source`: the shell part only (needs the shell function from `gopher shell-init zsh|bash`).
- `term`: lay out the window as sidebar plus agent.
- `sidebar <project>|--all [--replace|--restart]`: add or restart sidebars in open windows.
- `close` / `open` / `snapshot <project>`: close a window, saving its layout, pane roles, and
  Claude session ids to `<state_dir>/windows/<project>.json` (from tmux-resurrect and
  tmux-assistant-resurrect when present), and rebuild it later, resuming the agents.
- `where`, `link`, `unlink`: show and edit ownership of the cwd's checkout.
- With no project, a keypress menu; `shell-init` also installs completion for workon targets.

### Lifecycle (`gopher archive|close|reopen <project>`)

Scripts do the destructive parts; agents and Gopher only propose them. Each command prints a plan
and acts only with `--yes` (agents, after the user agrees) or a typed `yes` at a terminal. An open
window is snapshotted and closed first.

- **archive**: move to `<archived_dir>`; run `on_archive` in each checkout; keep the checkouts.
- **close**: move to `<closed_dir>`; for each owned git worktree not owned elsewhere and not
  `protected`, run `on_close`, `git worktree remove` (never forced), `git branch -d`, then
  `on_remove`. Uncommitted changes or commits on no remote block it; plain clones are kept. The
  `close` prompt has the agent check open PRs and offer ticket updates first.
- **reopen**: move back to `<projects_dir>`; checkouts are not recreated.

### Updates and restarts

Releases are `vX.Y.Z` tags on `[gopher] update_source`. `gopher update --check` lists them with
`git ls-remote`; `gopher update` installs the newest with `uv tool install --force` after a typed
`yes` (editable installs are told to `git pull`). When the nightly `update-check` task finds one,
Gopher mentions it once; on the user's yes it installs it, restarts the sidebars
(`gopher workon sidebar --all --restart`), and restarts itself last
(`gopher launch --restart --continue`, which resumes the conversation and restarts the
heartbeat).

### Health (`gopher doctor`)

`gopher doctor` checks the install and the root, one line per area, and exits non-zero on a
problem: the config parses and the state dir exists; every gopher hook is in
`~/.claude/settings.json` and points at an existing binary; tmux is running with gopher's config
loaded and the Gopher session up; the backup (repository, upstream, overdue); the schedule
(malformed tasks, failing commands) and heartbeat; checkouts that are missing or owned twice.
`-q` prints only warnings and problems.

### tmux integration

The tmux config ships in the package (`gopher shell-init tmux --path`); users add
`run-shell 'tmux source-file "$(gopher shell-init tmux --path)"'`. It binds `prefix g` to the
Gopher popup (toggling it closed from inside), runs `gopher launch` when a client attaches, and
runs `gopher launch --relink` when a window is unlinked, so Gopher moves back to index 0.
tmux-resurrect does not understand linked windows and may restore Gopher as a second copy; that
duplicate is not removed automatically (a known issue).

### Development reload

Opt-in for contributors (`[dev] reload = true` and the `dev` extra): every sidebar watches
`<state_dir>/tui/reload.json`. `gopher dev reload` smoke-starts the TUI headless and, if it
starts, publishes a token of the package's code; sidebars with a different token restart
themselves once no modal or input is open.

## Open questions

- A Stop-hook wake when the agent's turn ended with a plain-text question to the user: the
  deferral rule should hold, but it is not covered by a test.
- Fan-outs in `pending/` have no trigger of their own: Gopher follows up when an agent replies or
  the user asks. A tick event for stalled fan-outs is possible if that proves too quiet.
- Shell completion covers `gopher workon` targets, not the rest of the CLI.
