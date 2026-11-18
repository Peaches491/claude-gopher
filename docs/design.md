# claude-gopher: design

Status: design agreed 2026-11-12. Built so far: roots (`gopher new`, `gopher setup`, registry,
`checkouts.toml`), core prompts with `gopher prompt` / `gopher hook context`, inbox hooks,
session state hook + registry merge (component 2), `gopher projects` (data layer for the TUI).

Paths below use config keys (`<projects_dir>`, `<state_dir>`, `<agent_dir>`), relative to the
root; see "Workspace layout" and "Root configuration".

Gopher is a scribe/secretary agent. You tell it anything about any project; it routes the
information to the right project (inbox note, or a live message to the project's agent), keeps a
journal, answers status questions, and maintains `human/STATUS.md` on your behalf. A Textual TUI
next to it shows every active project at a glance.

## Decisions (from the interview)

| Topic | Decision |
|---|---|
| Placement | Window 0 of the main session and a `prefix g` popup, both showing the same window (owned by a `gopher` session, linked into the main session at index 0) |
| Layout | Gopher's window is the Claude Code Gopher session alone; the TUI runs as a sidebar in each project window (originally TUI left, Gopher right) |
| Delivery | Inbox by default; wake a live agent only when you say so ("tell X now") |
| Busy target | Queue in inbox, wake automatically when the agent goes idle |
| Pickup | Hook injects unread inbox items at session start and each prompt. Email model: the agent summarizes and the user decides; items never pull the agent off the user's request |
| Deferral | Default: start of session = summarize and ask what next; mid-session = one line at the end of the reply. Wake items are acted on via a subagent, unless the agent is waiting on your answer |
| Inbox store | `projects/$p/inbox/<ts>.md`, moved to `inbox/read/` once handled |
| Routing | Silent when confident; menu of candidate projects when not |
| human/STATUS.md | Gopher may append dated entries (preferred) and edit existing items (check off, update status) |
| Extra scope | Status Q&A, daily journal, create projects, fan-out to several projects |
| Summaries | YAML frontmatter at top of each `STATUS.md`; TUI can refresh via Haiku, or ask Gopher for a deep dive (Gopher may query an idle agent) |
| Agent Q&A | Native cross-session messaging (`SendMessage` reply); the agent also refreshes its STATUS.md frontmatter |
| Resources | `projects/$p/resources/<kind>/` with human-readable names, maintained by agents and Gopher |
| Open set | All projects under `<projects_dir>`; archived and closed projects live in `<archived_dir>` / `<closed_dir>` |
| Lifecycle | active -> archived (checkouts kept, build caches cleared) -> closed (checkouts deleted) |
| Gopher agent dir | `<agent_dir>` (not a project, so never routable) |
| Gopher perms | `--permission-mode auto`, prompt-scoped to project files, messaging, read-only lookups |

## Platform facts this relies on (docs research 2026-11-12)

- Cross-session messaging (`ListAgents` / `SendMessage`, docs: cross-session-messaging.md) is on by
  default (v2.1.224+), local Unix sockets only. `ListAgents` already reports each peer's name,
  state, and tmux pane (`0:@4.%13`). Idle receiver: message starts a new turn. Busy receiver: read
  between tool calls (so it does interrupt a working agent). Recipient can reply. Model-invoked
  only: scripts (the TUI) cannot call it; the raw socket wire format is undocumented, so do not
  post to sockets from scripts.
- No supported API exposes an interactive session's working/waiting/idle state. Durable option:
  hooks write a per-session state file. Events: SessionStart (pane, cwd, source), UserPromptSubmit
  (working), PermissionRequest / Notification `permission_prompt|elicitation_dialog` (waiting),
  Stop and Notification `idle_prompt` (idle), SessionEnd (gone).
- Hooks can inject context: `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`
  (10k char cap). Same for SessionStart.
- A Stop hook can return `{"decision":"block","reason":...}` to keep the agent going: this is how
  "wake when idle" works without anyone polling.

## Workspace layout

`workon` is the project system and the switcher/setup command; Gopher helps you work on your
separate projects. A deployment lives in a **root** (`$GOPHER_HOME`, default `~/workon`): a plain
directory meant to be its own git repo, like an Obsidian vault, separate from this source repo and
backed up or shared by the user. It holds everything for the deployment, customization included:

```
$GOPHER_HOME (~/workon)
  config.toml       root config (paths relative to the root)
  prompts/          site prompts, one per context (see "Prompts")
  projects/<p>/     STATUS.md, human/, plan/, notes/, inbox/, resources/, prompt/, script/, data/,
                    checkouts.toml
  archived/<p>/
  closed/<p>/
  gopher/           Gopher agent dir (<agent_dir>): prompt.md, journal/, pending/, STATUS.md
  reference/        shared reference library (<reference_dir>): playbooks, processes, key docs;
                    README.md index; curated by Gopher, read by project agents
  tmp/              regenerable, gitignored: tmp/<p>/, tmp/gopher/ (state dir)
  .gitignore        tmp/
```

Code (the `workon` and `gopher` commands) and the core prompts ship from this package. Git
checkouts live outside the root.

### Roots and discovery

Several isolated deployments can share a machine (e.g. work and personal). A small, per-machine,
unversioned registry names them, `$XDG_CONFIG_HOME/claude-gopher/config.toml`:

```toml
default = "work"
[roots]
work     = "~/workon"
personal = "~/gopher-personal"
```

The root is chosen by, in order: `--root` (a registry name or a path), `$GOPHER_HOME` (same),
the registered root containing the cwd, the registry's `default`, then `~/workon`. The registry holds nothing else; all other settings live
in the root's `config.toml`.

Sessions get their root from `$GOPHER_HOME`, which `workon` and `gopher launch` export for the
agents they start. A session started any other way uses the root containing its cwd, else the
default root. Each root has its own state dir, so `gopher agents` shows one deployment (`--all`
merges them).

### Creating a root (`gopher new`)

`gopher new [DIR] [--name NAME] [--from GIT_URL] [--default | --no-default]`

- `DIR` is prompted for when omitted (suggested: `~/workon`). It must be missing or empty; anything
  else is an error, and nothing is ever overwritten.
- Without `--from`: copy the packaged skeleton (`src/claude_gopher/skeleton/`) (`config.toml`, `prompts/`,
  `projects/`, `archived/`, `closed/`, `gopher/`, `.gitignore` excluding `tmp/`), create `tmp/`,
  `git init`, and commit the skeleton.
- With `--from`: `git clone` an existing root (e.g. one shared by a teammate) into `DIR`, then
  create `tmp/`.
- Register it in the machine registry under `NAME` (prompted; default the directory's basename).
- Make it the registry's `default` if it is the first root; otherwise ask (or obey the flag).

Projects inside a root are created with `gopher project new <name>` and renamed with
`gopher project rename <old> <new>`: it moves the directory, fixes the `STATUS.md` heading, carries
the inbox tracker and session records over to the new name, and renames tmux windows named after
the project. Running agents keep the old `GOPHER_PROJECT`; the hooks ignore a `GOPHER_PROJECT` that
names no project and fall back to the checkout, so they keep working until relaunched.

### Setting up a root (`gopher setup`)

`gopher setup [DIR] [--name] [--from GIT_URL] [--default|--no-default] [--no-hooks]
[--no-interview] [--review] [-y]` is the interactive front door; `gopher new` stays as the
scriptable primitive.

- `DIR` missing or empty: create it (or clone with `--from`), as `gopher new` does.
- `DIR` already a root (e.g. a clone on a new machine): repair it (create the state dir, ensure
  `/tmp/` in `.gitignore`).
- Either way: register it (keeping an existing registry name) and offer to make it the default.
- Hooks: show the `~/.claude/settings.json` entries it would add (`gopher hook state` for the state
  events, `gopher hook context` for SessionStart), and on yes back the file up and add them. Entries
  already present, under any gopher binary path, are left alone.
- Interview: offer to launch interactive `claude` in the root with the core `setup` prompt
  appended to its system prompt and the gopher install dir added (`--add-dir`), so it can read the
  script (`prompts/setup.md`), the core prompts, the skeleton, and (in a source checkout) this
  design. The first message is generated from the root's state: mode (new or review), the files to
  read, which site prompts already exist, and the project count. It interviews the user (repos, build and validation, ticket
  tracker, code review, credentials, lifecycle cleanup, anything else) with AskUserQuestion, writes
  `prompts/<context>.md` and `config.toml`, shows the diff, and commits on yes. Default yes for a
  new root, no for an existing one.
- `--review` skips straight to the interview on an existing root, revising its prompts.

Health checks (missing or doubly owned checkouts, missing config or prompts, stale hook binaries)
belong to a separate `gopher clean`, not setup.

### Checkouts

Checkouts are leaves: nothing is written into them, and they hold no reference to the project or
root that owns them. Ownership is recorded only in the root, in `projects/<p>/checkouts.toml`:

```toml
[[checkout]]
path = "~/src/monorepo-feature-x"
```

Moving a checkout to another project edits two `checkouts.toml` files and touches nothing in the
checkout. A session's project is resolved from `$GOPHER_PROJECT` (set by the launcher), else by
matching its cwd against every `checkouts.toml` in the root (longest path prefix wins). A checkout
listed by more than one project resolves to the first match; `gopher checkout add` warns when it
creates such an overlap.

Commands: `gopher checkout add <project> [path]`, `remove <project> [path]`,
`move <path> <project>` (drops it from every other owner), `list`; `gopher project new <name>
[--checkout path]`.

The author's earlier `~/ai_setup` (static/ shared rules, dynamic/projects/, bin/, shell/workon.py,
per-checkout `<git-dir>/ai_project` links) is a first attempt at this system. It is not supported
by gopher; it stays as is and is migrated separately later.

## Root configuration

The repo owns the opinionated project system (project layout, conventions, inbox, Gopher, TUI,
lifecycle). Everything specific to one deployment lives in `<root>/config.toml` and
`<root>/prompts/`. Nothing in the repo names a company, repo layout, or credential tool. Example
(only `[paths]` has built-in defaults, shown here; the rest is illustrative):

```toml
[paths]                       # relative to the root
projects_dir = "projects"
archived_dir = "archived"
closed_dir   = "closed"
agent_dir    = "gopher"
state_dir    = "tmp/gopher"
prompts_dir  = "prompts"

[checkouts]
provider  = "workspace"       # how to enumerate/create/delete checkouts (pluggable)
protected = ["~/src/monorepo"]

[[checkouts.setup]]           # run when entering a matching checkout (workon source/term/agent)
match = "~/src/monorepo*"     # glob on the checkout path
commands = ['eval "$(my-env-tool activate {{name}})"', "git status -sb"]   # {{path}} {{name}} {{project}} {{root}}

[workon]
default_agent = "claude"
pre_launch = ["type auth-try >/dev/null 2>&1 && auth-try"]   # run before the agent starts
default_checkouts = { shared = "~/src/monorepo" }

[agents.claude]               # built in; override the command here
command = ["claude", "--permission-mode", "auto"]

[agents.cursor]
command = ["cursor-agent", "--auto-review"]
resume_flags = ["--resume"]   # passing one of these skips the kickoff prompt

[lifecycle]
on_archive = ["make clean"]
on_close   = ["make clean"]
```

`gopher new` writes a generic starting root from the packaged skeleton.

## workon

`gopher workon` opens a project. `gopher` is the only entry point; users who want the short name
add `alias workon='gopher workon'`.

- `agent [--agent NAME] [project [checkout]] [-- agent args]`: cd to the checkout (or the project
  dir when it has none), export `GOPHER_HOME` and `GOPHER_PROJECT`, rename the tmux window, run the
  matching `[[checkouts.setup]]` commands and `[workon] pre_launch`, then start the agent with the
  session kickoff prompt (skipped when resuming).
- `term`: lay out the tmux window: `human/STATUS.md` editor (auto-reloading in vim), the agent
  pane, and this shell sourced into the project.
- `source`: the shell part of `agent` only. Changing the caller's directory needs the shell
  function from `eval "$(gopher shell-init zsh|bash)"`, which wraps `gopher` and evals the script
  for `workon source|agent|term`. Without it, `agent` runs the script in `$SHELL` and execs the
  agent.
- `where [project]`, `link <project>`, `unlink <project>`: show and edit `checkouts.toml` using the
  cwd's git checkout.
- No project given: a keypress menu (the cwd checkout's owners, else every project, plus
  "+ new project"). Checkout: the argument (name or path), else `default_checkouts`, else the cwd
  checkout if owned, else the only owned one, else a menu; a project with none offers to adopt the
  cwd checkout.
- `shell-init` also installs completion: `gopher` subcommands, and `workon` actions, projects
  (with summaries), agents, and checkouts.

## Prompts

Gopher's behaviour in each context is driven by a prompt. The repo ships a generic core prompt per
context (`src/claude_gopher/prompts/<context>.md`) that talks about repos, the build system,
tickets, and pull requests without naming any tool. A root adds its own prompt per context in
`<root>/prompts/<context>.md`. Assembly is append
only: core text, then site text under a `## Site` heading. A root cannot remove core rules; it can
refine them in prose. A missing file just means core only.

| Context | Used by | Core covers | Example site content |
|---|---|---|---|
| `all` | every agent | project layout, STATUS.md, human/ rule, generic repo/build/ticket/PR vocabulary | main repo path and layout, build system, ticket tracker and key format, PR tooling, credentials |
| `gopher` | Gopher agent | routing, journal, inbox delivery, authority over human/STATUS.md | project naming hints, team names |
| `project` | project agents | inbox handling, frontmatter upkeep, resources upkeep | test/validation commands |
| `resources` | agents + Gopher | resources/ layout and file format, generic kinds (tickets, prs, docs, dashboards, explainers) | site kinds and URL patterns (ticket tracker, forge, dashboards) |
| `archive` / `close` / `reopen` | agent proposing it, Gopher, lifecycle scripts' plan output | what the transition means, guardrails, asks before acting | ticket transitions, cache clearing, protected checkouts |
| `inbox` | inbox inject hook | how an injected item is framed, deferral rule | none expected |
| `tui-summary` | TUI `r` (Haiku) | summary format and length | site jargon to keep or expand |
| `setup` | `gopher setup` interview | interview topics, which file each answer goes to | none expected |

Delivery:
- `gopher prompt <context> [--project P]` prints the assembled prompt (also for debugging a
  site's setup). Prompts may use `{{root}}`, `{{projects_dir}}`, `{{archived_dir}}`,
  `{{closed_dir}}`, `{{agent_dir}}`, `{{tmp_dir}}`, `{{project}}`, `{{project_dir}}`,
  `{{project_tmp}}`.
- `gopher hook context` (SessionStart) injects `all` plus `project` when the session resolves to
  a project (`$GOPHER_PROJECT`, a cwd inside `projects/<p>/`, or an owned checkout), or `all` plus
  `gopher` for the Gopher session (`$GOPHER_AGENT=gopher` or a cwd inside `<agent_dir>`). Other
  sessions get nothing, as do roots without `config.toml`. Output is capped at 10k characters, with
  a truncation note pointing at `gopher prompt`.
- `resources` and the lifecycle prompts are on demand: the core `project` prompt tells agents to run
  `gopher prompt <context>` before adding resources or proposing a transition.

Employer-specific customization (a particular monorepo, build system, ticket tracker, PR tooling,
auth helpers) lives in that deployment's root, versioned and shared with its git repo. None of it
is in this repo.

## Components

### 1. Conventions (core prompts `all.md` / `project.md` / `resources.md`)

- **STATUS.md frontmatter**, kept current by the project agent:
  ```yaml
  ---
  summary: One sentence on where the project stands.
  state: active | blocked | waiting-review | closing
  updated: 2026-11-12
  ---
  ```
- **resources/**: `resources/tickets/<KEY>.md`, `resources/prs/<number>.md`,
  `resources/docs/<slug>.md`, `resources/explainers/<slug>.md`, `resources/dashboards/<slug>.md`, ...
  Short durable ids (ticket key, PR number) are the filename; everything else gets a readable slug.
  First line `# [Title](url)`, then a short blurb on the resource's role in the project. Agents add
  resources as they create or discover them; Gopher fills gaps.
- **Inbox handling**: items arrive via hook. Act on them, fold them into STATUS/plan/notes, then
  move the file to `inbox/read/`. If mid-discussion with the user or deep in another thread, defer
  (leave the file) and mention it at a natural break. "Defer" from the user always wins.
- **Gopher's authority** over `human/STATUS.md` (amends the current "never write human/" rule:
  Gopher only, and only on your behalf).
- **Lifecycle**: when all to-dos are resolved or the scope is done, an agent or Gopher may ask
  whether to archive or close; nothing moves without your yes.

### 2. Session state hook (`gopher hook state`)

Writes `<state_dir>/sessions/<session_id>.json`:
`{session_id, state, event, pane, tmux_window, cwd, project, transcript_path, updated}`. `project`
is resolved as in "Checkouts" (`$GOPHER_PROJECT`, else cwd matched against `checkouts.toml`). Removed on SessionEnd. `gopher agents` prunes files whose
pane is gone and keeps only the newest session per pane. The window-glyph hook can later read the
same state so both stay consistent.

Claude Code's own per-process registry (`~/.claude/sessions/<pid>.json`: sessionId, cwd,
`tmux` "0:@win.%pane", name, status busy/idle/waiting/shell) is merged in: it decides which sessions
are live (pid + `procStart` vs `/proc/<pid>/stat`) and gives a baseline state, so sessions started
before the hook existed still appear; `shell` (idle with background shells) maps to `background`.
Hook records add the project and win when newer. The registry is undocumented; if it disappears,
gopher falls back to hook records with tmux pane pruning.

Event -> state: SessionStart, Stop, Notification `idle_prompt` -> `idle`; UserPromptSubmit,
PreToolUse, PostToolUse -> `working`; PreToolUse `AskUserQuestion`, PermissionRequest, Notification
`permission_prompt|elicitation_dialog|agent_needs_input` -> `waiting`. The hook never fails the
session: errors go to `<state_dir>/errors.log` and it exits 0.

### 3. Inbox hooks

- **File**: `gopher inbox add <project> [--wake] [--from NAME] [text...]` (stdin when no text)
  writes `inbox/<YYYYMMDD-HHMMSS>-<slug>.md`. `gopher inbox list [project]` shows unread items.
- **Inject**: `gopher hook context` (SessionStart) appends the project's unread `inbox/*.md` to the
  session context, and `gopher hook inbox` (UserPromptSubmit) adds them before each prompt. Full
  text the first time a session sees an item, a one-line "N deferred inbox items" reminder
  afterwards. Per-session tracking lives in `<state_dir>/inbox/<project>/<session_id>.seen`, not in
  the root, and resets on SessionStart `compact` / `clear`. Capped at 8k characters.
- **Email model**: at session start the agent replies with where the project stands and one line per
  inbox item, then asks what to do; it does not start on an item unless the user picks it.
  Mid-session it finishes the user's request and names new items in one line at the end.
- **Wake**: `gopher hook inbox` (Stop): if an unread item has `wake: true` and this session has not
  been woken for it (`.woken`), block the stop with the item text. The agent hands it to a subagent
  (keeping its own context clean), which does the work, records anything durable in the project,
  and moves the item to `inbox/read/`; the agent then reports in a line or two. If it was waiting on
  the user's answer, it only mentions the item.
- **Visible notice**: the SessionStart hook also returns a `systemMessage` shown to the user
  (`gopher: project <p>, N unread inbox item(s)`), so a session's project and inbox are visible
  before the first prompt.
- **Kickoff**: `workon agent` will start sessions with "Start of session: read your status,
  summarize your inbox, and ask me what I'd like to do next." (`inbox.SESSION_KICKOFF`). Sessions
  started by hand get the same behaviour on their first message.
- `gopher setup` installs `hook inbox` for UserPromptSubmit and Stop.

Inbox item format:
```markdown
---
from: gopher
created: 2026-11-12T14:03
wake: false
---
<your words, verbatim, then Gopher's one-line routing note if useful>
```

### 4. Gopher agent

- Agent dir `<agent_dir>`: `prompt.md` (system/launch prompt), `journal/<date>.md` (everything you
  said, where it went), `pending/` (wake requests awaiting idle), `STATUS.md`.
- Prompt: core `prompts/gopher.md` (role, voice, what Gopher owns and must not do, routing,
  fan-outs tracked in `pending/`, tickets/Slack/Docs, end-of-day and weekly rituals) plus
  `gopher-proactivity-<level>.md` for `[gopher] proactivity` (`quiet` default, `nudge`,
  `proactive`; `gopher proactivity [level]` or TUI `P` sets it and tells the running Gopher) and
  one `gopher-feature-<name>.md` per `[gopher] features` entry (`notebook`: daily
  `notebook/YYYY-MM-DD.md`; `changelog`: shipped things in `changelog.md`). Site-specific facts
  (default ticket project) go in the root's `prompts/gopher.md`. `gopher launch` writes the
  assembled text to `<state_dir>/gopher-prompt.md` and passes it with
  `--append-system-prompt-file` (no 10k hook-context cap, survives `/clear`); the SessionStart hook
  then only points at it.
- Launched by `gopher launch` as `claude --model sonnet --name gopher --permission-mode auto ...`
  (model from `[gopher] model`, default `sonnet`; `""` leaves it to Claude Code's default) in the `gopher`
  tmux session, so peers can message it by name. `gopher launch [--session S]` is idempotent: it
  creates the session if missing, then links its window at index 0 of `S` (default: the current
  session), refusing if another window holds index 0. `examples/tmux.conf` runs it from a
  `client-attached` hook. Project agents keep Claude's generated names; `gopher agents` shows the
  name to address.
- On start: reads frontmatter + `human/STATUS.md` of every active project, and the state files.
- Routing: one or more projects; confident -> deliver and report one line; unsure -> menu.
- "Tell X now": target idle (state file / `ListAgents`) -> `SendMessage`; busy -> inbox item with
  `wake: true` (Stop hook delivers it).
- Creating projects: runs `gopher project new <name>` (makes `<projects_dir>/<name>/` with
  STATUS.md frontmatter); offers to open a `workon term` window.
- Deep summary: messages the idle agent ("refresh your STATUS.md frontmatter and reply with a
  summary"), waits for the reply.

### 5. TUI (`gopher tui`, Python + Textual)

- Table of active projects: agent state icon (from state files), frontmatter summary, PRs open/merged
  (from `resources/prs/*.md`, live state via the forge CLI, cached), ticket count, last-updated age.
  Blocked-on-you rows sorted to the top.
- Watches `<projects_dir>/*/STATUS.md`, `resources/`, and the state dir (inotify/polling).
- Keys: `enter` prefill `[project: X] ` into the Gopher pane and focus it; `r` refresh summary
  via `claude -p --model haiku` (writes to TUI cache only, not STATUS.md); `R` ask Gopher for a
  deep dive; `t` jump to the project's tmux window.
- Sends to Gopher with `tmux send-keys` (no Enter for prefill), since scripts cannot use
  cross-session messaging.
- First pass (2026-11-12, `src/claude_gopher/tui.py`): polls `project_overview` every 5 s (no
  inotify). Each project is a 5-row block (herdr-sidebar style) with a thick left bar coloured by
  its most urgent agent. Row 1: name and age, with the project's frontmatter state right-aligned.
  Row 2: envelope and unread inbox count (`!` for wake items) and resource links (OSC 8), with one
  aggregate agent status right-aligned (most urgent agent's icon and label, `+N` for more agents;
  no pane locations). Rows 3-5: the summary. Resource files carry optional frontmatter (`state`,
  `merged`, `stack`; see the resources prompt): the card hides closed/resolved/done tickets and
  closed PRs, shows merged PRs for 24 h with a check mark (a date-only `merged` counts until the
  end of the next day), and collapses each stack to its top visible PR with the stack size in
  parentheses. Files without frontmatter count as open. The expanded view lists every resource
  with its state.
- Ranking (`overview.project_rank`, shared with `gopher projects`) follows the card colours:
  yellow (an agent needs input, or status `waiting-review`), then red (an agent needs login, or
  `blocked`), then green (`active`), then grey (anything else, e.g. `closing`). Agent state only
  lifts a project into yellow or red. Within a colour: most urgent agent first (needs input,
  needs login, done, working, background, idle), then longest-waiting first for needs input and
  login, most recent first otherwise; projects with no agent by STATUS.md date. The left bar and
  the right-aligned status use the status colour; "done" agents show green.
- "Done" is acknowledged when a tmux client keeps the agent's pane focused for `[tui] seen_dwell_seconds` (default 5) after it
  finished (the TUI samples `list-clients` on each 5 s poll), or with `x`. Acks live in
  `<state_dir>/tui/acks.json`; hook records carry `finished` (set on `Stop`, cleared by the next
  non-idle event). Known gap: an outer client with the popup open still counts as viewing the pane
  under the popup.
- Keys: `j`/`k` move, `g`/`G` top/bottom, `l`/`enter` expand, `t` jump to the agent
  (`switch-client`, best pane first), `x` mark done as seen, `m`/`M` file an inbox item (`M` with
  wake) from a one-line prompt, `r` one-line Haiku summary (cached in `tui/summaries.json`, `~`
  prefix, dropped once STATUS.md is newer), `ctrl+r` refresh, `q` quit. `i` (prefill
  `[project: X] ` into Gopher's pane) and `R` (deep dive via Gopher) are only offered when the TUI
  runs in the Gopher window.
- Expanded view: live facts (agents with name, state, location; unread inbox items; PR/ticket
  links; checkouts), a multi-line Haiku summary (`tui-detail` prompt, cached in
  `tui/details.json` like the one-liner), then STATUS.md rendered as Markdown. `j`/`k` scroll,
  `h`/`esc` back; it returns to the list after 90 s without a key.
- Direction: the TUI grows quick actions run by Haiku (`claude -p`) for small, well-scoped jobs,
  and hands anything multi-step to Gopher (e.g. "summarize this Slack thread, open a ticket, and
  send it as an inbox item to a project").

### 5b. Sidebar (2026-11-13)

- `gopher workon term` lays a project window out as two panes: the TUI as a full-height left
  column (20%) and the agent (80%). `gopher workon close|open|snapshot <project>` closes a project
  window and brings it back: close takes a fresh tmux-resurrect save, copies that window's slice
  (layout string, pane dirs and roles, Claude session ids from tmux-assistant-resurrect) to
  `<state_dir>/windows/<project>.json`, and kills the window (resurrect's later saves no longer
  contain it, so the copy is the record); open rebuilds panes in order, applies the layout, starts
  the sidebar and `gopher workon agent ... -- --resume <id>`, and other panes come back as shells.
  The sidebar's `o`/`c` keys call the same code.
  `gopher workon sidebar <project>|--all [--replace]` adds the
  sidebar to open windows (`--replace` closes their shell/editor panes, never other programs).
- `gopher tui` pins the pane's own project (agent in the pane, then cwd, then window name;
  `--project` overrides) in a header: summary, cached Haiku line, each agent's state and name,
  inbox and links, and buttons with hotkeys: custom actions (`[[tui.actions]]` in the root
  `config.toml`: key, label, shell command, optional `match` glob on checkout paths that hides the action from
  projects without a matching checkout, run in the first (matching) checkout or the project directory,
  in a 90% tmux popup so it gets a real terminal's colour and width), `b` inbox list, `n` new inbox item, `u` ask the agent beside it to refresh
  STATUS.md, `e` edit `human/STATUS.md` in a popup. The inbox list: `enter` prefills the agent
  beside it with `Inbox item inbox/<file>: `, `x` moves the item to `inbox/read/`, `l` shows it.
  Other projects follow as compact blocks: name and status, links and agents, then the summary
  (or Haiku line) cut to `[tui] summary_lines` rows (default 2; 0 hides it). Without a pinned project it is the full
  dashboard.
- Developer reload (opt-in, `[dev] reload = true`, needs the `dev` extra for watchfiles): each
  sidebar watches `<state_dir>/tui/reload.json` with inotify. `gopher dev reload` smoke-starts the
  TUI headless and, if it starts, writes a token (hash of the package's code) plus the git SHA
  there; sidebars whose own token differs restart themselves (re-exec, same arguments) once no
  modal or input is open. Off by default; regular installs never watch or reload.
- Idle reset: after `[tui] idle_seconds` (default 60; 0 off) without input, a pinned sidebar moves
  its highlight back to the first project and focus to the header, unless a modal, an input, or
  the inbox list is open.
- Saving the root: Gopher owns the root repository's history. `gopher save` groups `git status`
  paths by project directory (projects/, archived/, closed/), commits each group with a message
  naming the areas touched, commits the rest as `root: ...`, and pushes. The `save` schedule task runs it (section 9);
  Gopher also runs it at wrap-up and on request.
- Priority: 0-3 stars in `human/STATUS.md` frontmatter (`priority:`; the user's judgment, so
  never set by project agents), set with `gopher project priority` or the TUI's `+`/`-`. Starred
  projects sort first (stars, then colour group) under a "Priority" label, shown as stars before
  the name. `/` filters the list by name: letters in order, or a glob.
- Keys on a highlighted project: `enter`/`t` jump to it, `l` expand, `g` popup on Gopher with
  `[project: X] ` prefilled, `i` popup on that project's agent with `[from: <pinned>] `
  prefilled (no live agent: the inbox prompt), `m`/`M` inbox item, `home`/`end` top/bottom.
- Popups (`popup.py`, `gopher popup gopher|agent`, also behind `prefix g`): dim the window
  behind, record the outer client, origin and target panes in `@gopher_popup_*`. Agent popups
  attach through a temporary grouped session (`gopher-peek-*`, destroyed when the popup closes)
  so the outer client's current window does not move, and zoom the agent pane meanwhile. A popup
  opened with prefilled text closes itself when that agent receives the prompt (the state hook's
  `UserPromptSubmit` detaches the popup clients).
- The Gopher window is just the Gopher agent.

### 6. tmux integration

- The Gopher window is owned by a dedicated `gopher` session and linked into the main session at
  index 0 (`link-window -s gopher:0 -t 0:0`): one window, one Claude process, visible in both places.
- `prefix g` (replacing tmux 3.8's default floating-pane `move` table) toggles a popup attached to the `gopher` session; the same key inside the popup detaches:
  ```
  bind g {
    if -F '#{==:#{session_name},gopher}' { detach-client } {
      run-shell -b 'tmux set -w -t "#{window_id}" window-style dim=40; tmux set -g @gopher_popup_client "#{client_name}"; tmux set -g @gopher_popup_pane "#{pane_id}"; tmux display-popup -c "#{client_name}" -E -s bg=terminal -S bg=terminal -w 90% -h 90% "TMUX= tmux attach -t gopher; tmux -S #{socket_path} set -wu -t #{window_id} window-style; tmux -S #{socket_path} set -gu @gopher_popup_client; tmux -S #{socket_path} set -gu @gopher_popup_pane"'
    }
  }
  ```
  The window behind the popup is dimmed while it is open (tmux has no built-in backdrop for popups).
  (No-op, or select-window, when already on window 0.)
- While the popup is open the window resizes to the popup and Claude Code redraws; harmless.
- tmux-resurrect does not understand linked windows and may restore Gopher as two windows.
  `gopher launch` is idempotent (ensure `gopher` session, kill duplicates, re-link at 0) and runs from
  a tmux startup / post-restore hook.
- Ships as `examples/tmux.conf` to source from the user's tmux config.

### 7. Lifecycle commands (`gopher archive|close|reopen <project>`) **(done)**

Scripts, not agents, do the destructive parts; agents/Gopher only propose them.
- **archive**: move to `<archived_dir>/<project>/`; for each linked checkout run the site's
  `on_archive` commands (e.g. clear the build cache), keep the checkout.
- **close**: move to `<closed_dir>/<project>/`; delete linked checkouts via the site's checkout
  provider (plus `on_close` cleanup such as build output dirs), with guardrails:
  - skip checkouts linked to another project, and any `protected` checkout
  - block on uncommitted changes, unpushed commits, or open PRs
  - print the full plan (worktrees, branches, output bases) and require an explicit `yes`
  - offer to update each ticket in `resources/tickets/` (transition/comment), via the agent, as the
    `close` prompt describes
  - as built: worktrees only (plain clones are kept), `git worktree remove` without force, then
    `git branch -d`, then the site's `on_remove` commands (run in the main checkout with
    `GOPHER_CHECKOUT` set); blockers are uncommitted changes and commits on no remote branch (open
    PRs are left to the agent's pre-close check); an open window is snapshotted and closed first.
    Agents run the plan, show it, and pass `--yes` only after the user agrees; at a terminal the
    user types `yes`.
- **reopen**: move the project dir back to `<projects_dir>` (checkouts are not recreated).

### 8. Updates and restarts

Releases are `vX.Y.Z` tags on the source repo (`[gopher] update_source`). `gopher update --check`
lists them with `git ls-remote`; `gopher update` installs the newest with
`uv tool install --force git+<source>@<tag>` after a typed `yes` (editable installs are told to
`git pull`). The `update-check` schedule task runs the check nightly (section 9; seeded when `[gopher] update_check` is on),
mentions a new version once, and on the user's yes installs it, restarts the sidebars
(`gopher workon sidebar --all --restart`, which respawns each sidebar pane), and restarts itself
last (`gopher launch --restart --continue`, which respawns the Gopher pane with `claude --continue`).
`gopher setup --remove-hooks` undoes the hook install. The tmux config ships in the package;
`gopher shell-init tmux --path` locates it so tmux sources the installed copy.

### 9. Schedule and heartbeat (`gopher schedule`)

Gopher only acts when a message arrives, and Claude Code cron jobs are session-only and expire
after 7 days, so recurring work described only in the prompt was lost on every restart and left
nudges and scans with no trigger. The schedule moves it into data:

- `schedule/<task>.md` in the root (versioned, user-editable): frontmatter `every` (10m/2h/1d) or
  `at` (HH:MM local, daily), optional `days`, `run`, `until`, `enabled`; the body is instructions
  for Gopher. New roots get `README.md`, `save.md`, and `update-check.md` (`[gopher]
  save_every_hours` and `update_check` pick the defaults); `gopher new` and `gopher setup` ask in
  a terminal whether to keep, retime, or drop each and which tasks to add, and the setup interview
  reviews it. Existing roots get the defaults on their first tick.
- `gopher schedule tick` (under a lock) runs every due task's `run` command in the root, records
  last runs in `tmp/gopher/schedule.json`, and prints one section per due task (command output,
  then the body), plus events: backup newly overdue, and at the proactive level projects whose
  STATUS `updated` is five or more weekdays old (each reported once per episode). An `at` task
  first seen after its slot waits for the next one; a missed slot runs once on the next tick.
  Ended (`until`) tasks and malformed files are reported once. No output means nothing to do.
- The heartbeat is one session-only CronCreate job (every 10 minutes at off-minutes) that the
  kickoff and restart kickoff tell Gopher to create; it runs the tick and acts on the output.
  `gopher launch --exec-agent` records the start, and the tick asks Gopher to recreate the job
  after six days, ahead of the 7-day expiry.
- `gopher schedule` lists tasks; the TUI's bottom line shows the last tick and next task due,
  yellow once the heartbeat has been silent for 30 minutes.

## Build order

0. Roots: `gopher new`, `gopher project new`, registry, `$GOPHER_HOME` discovery, root `config.toml`, `checkouts.toml` project
   resolution (replaces per-checkout link files). **(done)**
1. Core prompts (frontmatter, resources/, inbox, Gopher authority, lifecycle) + prompt assembly
   (`gopher prompt`, SessionStart injection). **(done)** `gopher setup` with hook install and the
   agentic interview. **(done)** `gopher clean` (health checks) later.
2. State hook. **(done)** Then switch the window glyph hook to it.
3. Inbox hooks (inject + Stop wake), tested against one project. **(done)**
4. Gopher prompt + `gopher launch` + popup binding. **(built; session and popup run live, window 0
   link not yet confirmed by the user)**
5. TUI. **(done)** `gopher tui`; since the sidebar (5b) it runs in each project window, not beside Gopher.
6. `gopher archive|close|reopen`. **(done)**
7. `gopher workon` (agent, term, source, where, link, unlink), absorbing `workon.py` /
   `runagent` from `~/ai_setup`. **(done)** Then migrate `~/ai_setup` data into `~/workon`.
   Shell completion for the rest of `gopher` is a to-do.

Each step is usable on its own; 1-3 already help without Gopher (you can drop notes into an inbox).

## Open questions / to verify

- ~~`crossSessionInbound` with auto-mode receivers~~: verified 2026-11-12 (Claude Code 2.1.323).
  Auto-mode sender to auto-mode receiver is delivered without approval and the receiver can reply.
  Messages are held for approval only when the sender's permission mode differs from the
  receiver's, so Gopher and project agents must share a mode (both `auto` by default).
- Whether AskUserQuestion fires PermissionRequest or Notification (needed for the "waiting" state).
- Stop-hook wake when the agent's turn ended with a question to you in plain text: the deferral rule
  should handle it, but verify it doesn't steamroll the conversation.
- Migrating existing projects: backfill frontmatter and resources/ (Gopher's first job?).
- Verify popup + linked-window behaviour (nested client key handling, resize) before building.
- Build cache clearing is site config (`on_archive` / `on_close`); the author's site decides
  between per-checkout clean and deleting output bases.
