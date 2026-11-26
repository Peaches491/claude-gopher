import datetime
import fnmatch
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, ListItem, ListView, Markdown, OptionList, Rule, Static
from textual.widgets.option_list import Option

from claude_gopher.config import (
    DEFAULT_IDLE_SECONDS, DEFAULT_SEEN_DWELL_SECONDS, DEFAULT_SUMMARY_LINES, PROACTIVITY_LEVELS, Config, ConfigError, CustomAction, gopher_proactivity, load_config,
    idle_seconds, seen_dwell_seconds, set_gopher_setting, sidebar_summary_lines, tui_actions,
)
from claude_gopher import devreload, doctor, lifecycle, popup, restore, schedule
from claude_gopher.checkouts import read_checkouts
from claude_gopher.inbox import add_item, mark_read, unread
from claude_gopher.launch import heal_schedule, write_prompt
from claude_gopher.overview import (
    CREDS, DONE, Acks, Resource, current_sessions, effective_state, most_urgent, open_tickets, project_overview,
    visible_prs,
)
from claude_gopher.projects import read_frontmatter, write_priority
from claude_gopher.prompt import assemble
from claude_gopher.registry import BACKGROUND
from claude_gopher.rootgit import WARN_ICON, BackupStatus, backup_status
from claude_gopher.state import IDLE, LEADING_GLYPHS, WAITING, WORKING, resolve_project
from claude_gopher.workon import status_editor_command

REFRESH_SECONDS = 5
BACKUP_REFRESH_SECONDS = 60
BACKUP_STYLES = {"ok": "bright_black", "overdue": "yellow", "local": "yellow", "error": "red"}
CHANGE_STYLES = {"added": "green", "removed": "red", "changed": "yellow"}
RELOAD_CHECK_SECONDS = 2
RELOAD_ENV = "GOPHER_TUI_RELOADS"
DETAIL_TIMEOUT = 90
SUMMARY_MODEL = "haiku"
SUMMARY_TIMEOUT = 120
PINNED_ACTIONS = ("custom", "toggle_inbox", "note", "refresh_status", "edit_human", "quit_tab")
REFRESH_STATUS_PROMPT = "Please refresh STATUS.md (frontmatter and body) so it reflects where this project stands now."
STATE_ORDER = {WAITING: 0, CREDS: 0, DONE: 0, WORKING: 1, BACKGROUND: 1, IDLE: 2}
STATE_STYLE = {
    WAITING: "bold yellow", CREDS: "bold red", DONE: "bold green", WORKING: "green", BACKGROUND: "green", IDLE: "dim",
}
STATE_LABEL = {WAITING: "needs input", CREDS: "needs login"}
STATE_ICON = {
    WAITING: "\u25cf", CREDS: "\u2715", DONE: "\u2714", WORKING: "\u2605", BACKGROUND: "\u25d0", IDLE: "\u25cb",
}
PRIORITY_ICON = "\u2605"
INBOX_ICON = "\u2709"
DONE_ICON = "\u2714"
PROJECT_STATE_COLOUR = {"active": "green", "blocked": "red", "waiting-review": "yellow", "closing": "bright_black"}
STATE_CLASSES = tuple(f"pstate-{state}" for state in PROJECT_STATE_COLOUR) + ("pstate-other",)
RESOURCE_PREFIX = {"prs": "#", "tickets": ""}


def updated_age(updated: str | None, today: datetime.date | None = None) -> str:
    if not updated:
        return "-"
    try:
        day = datetime.date.fromisoformat(updated)
    except ValueError:
        return updated
    days = ((today or datetime.date.today()) - day).days
    return "today" if days <= 0 else f"{days}d"


def state_text(state: str) -> Text:
    return Text().append(f"{STATE_ICON.get(state, '?')} {STATE_LABEL.get(state, state)}", style=STATE_STYLE.get(state, ""))


HEALTH_LINES = 3
HEALTH_STYLE = {"fail": "red", "warn": "yellow"}


def gopher_state(config: Config) -> str | None:
    pane = schedule.gopher_pane()
    if not pane:
        return None
    session = next((s for s in current_sessions(config, prune=False) if s.get("pane") == pane), None)
    if not session:
        return None
    return CREDS if session.get("blocked_creds") else session.get("state")


def health_text(state: str | None, problems: list, width: int) -> Text:
    text = Text("gopher ", style="bright_black")
    text.append_text(state_text(state) if state else Text("not running", style="red"))
    shown = problems if len(problems) <= HEALTH_LINES else problems[:HEALTH_LINES - 1]
    for check in shown:
        line = Text(f"{WARN_ICON} {check.area}: {check.message}", style=HEALTH_STYLE.get(check.level, "yellow"))
        line.truncate(width, overflow="ellipsis")
        text.append("\n").append_text(line)
    if len(shown) < len(problems):
        text.append(f"\n+{len(problems) - len(shown)} more: gopher doctor (D dismisses)", style="yellow")
    return text


def agents_text(sessions: list[dict]) -> Text:
    top = most_urgent(sessions)
    if not top:
        return Text("no agent", style="dim")
    text = state_text(effective_state(top) or "?")
    if len(sessions) > 1:
        text.append(f" +{len(sessions) - 1}", style="dim")
    return text


def inbox_text(inbox: dict) -> Text:
    if not inbox["unread"]:
        return Text()
    return Text(f"{INBOX_ICON} {inbox['unread']}{'!' if inbox['wake'] else ''}", style="bold" if inbox["wake"] else "")


def link(label: str, url: str | None, style: str = "") -> Text:
    return Text().append(label, style=Style.parse(style or "none") + Style(link=url, underline=True) if url else style)


def links_text(links: dict[str, list[Resource]], now: float | None = None) -> Text:
    text = Text()
    for pr, count, ended in visible_prs(links.get("prs", []), now):
        if text:
            text.append(" ")
        if ended == "merged":
            text.append(f"{DONE_ICON}", style="green")
        text.append_text(link(f"#{pr.name}", pr.url, "strike dim red" if ended == "closed" else ""))
        if count > 1:
            text.append(f"({count})", style="dim")
    for ticket in open_tickets(links.get("tickets", [])):
        if text:
            text.append(" ")
        text.append_text(link(ticket.name, ticket.url))
    return text


def resource_list(kind: str, items: list[Resource]) -> Text:
    text = Text(f"{kind} ", style="dim")
    for i, item in enumerate(items):
        if i:
            text.append(", ")
        text.append_text(link(f"{RESOURCE_PREFIX.get(kind, '')}{item.name}", item.url, "dim" if item.closed else ""))
        if item.state != "open":
            text.append(f" {item.state}", style="dim")
    return text


def jump_target(sessions: list[dict]) -> str | None:
    with_pane = [s for s in sessions if s.get("pane")]
    if not with_pane:
        return None
    best = min(with_pane, key=lambda s: (STATE_ORDER.get(effective_state(s), 3), -s.get("updated", 0)))
    return best["pane"]


class SummaryCache:
    def __init__(self, config: Config, name: str = "summaries"):
        self.path = config.state_dir / "tui" / f"{name}.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, project: dict) -> str | None:
        entry = self.load().get(project["name"])
        if not entry:
            return None
        status = Path(project["path"]) / "STATUS.md"
        if status.is_file() and status.stat().st_mtime > entry.get("at", 0):
            return None
        return entry.get("summary")

    def put(self, name: str, summary: str) -> None:
        data = self.load()
        data[name] = {"summary": summary, "at": time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))


def summary_input(project_dir: Path) -> str:
    parts = []
    for rel in ("STATUS.md", "human/STATUS.md"):
        path = project_dir / rel
        if path.is_file():
            parts.append(f"<{rel}>\n{path.read_text()}\n</{rel}>")
    return "\n\n".join(parts)


def summarize(config: Config, project_dir: Path, context: str = "tui-summary", flatten: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "GOPHER_PROJECT", "GOPHER_AGENT")}
    result = subprocess.run(
        ["claude", "-p", "--model", SUMMARY_MODEL, assemble(config, context)],
        input=summary_input(project_dir), capture_output=True, text=True, timeout=SUMMARY_TIMEOUT,
        cwd=config.state_dir if config.state_dir.is_dir() else config.root, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"claude exited {result.returncode}")
    return " ".join(result.stdout.split()) if flatten else result.stdout.strip()


def tmux(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *argv], capture_output=True, text=True)


def agent_pane_beside(own_pane: str | None) -> str | None:
    if not own_pane:
        return None
    listing = tmux("list-panes", "-t", own_pane, "-F", "#{pane_id}\t#{pane_current_command}")
    if listing.returncode != 0:
        return None
    for line in listing.stdout.splitlines():
        pane, _, command = line.partition("\t")
        if pane != own_pane and command == "claude":
            return pane
    return None


def pane_project(config: Config, pane: str, projects: dict[str, dict]) -> str | None:
    for name, project in projects.items():
        if any(s.get("pane") == pane for s in project["sessions"]):
            return name
    info = tmux("display-message", "-p", "-t", pane, "#{pane_current_path}\t#{window_name}")
    if info.returncode != 0:
        return None
    path, _, window = info.stdout.rstrip("\n").partition("\t")
    if (project := resolve_project(path, config.projects_dir, {})) in projects:
        return project
    window = LEADING_GLYPHS.sub("", window)
    return window if window in projects else None


def next_level(level: str) -> str:
    return PROACTIVITY_LEVELS[(PROACTIVITY_LEVELS.index(level) + 1) % len(PROACTIVITY_LEVELS)]


def focused_panes() -> dict[str, str]:
    listing = tmux("list-clients", "-F", "#{client_name}\t#{pane_id}\t#{window-style}")
    if listing.returncode != 0:
        return {}
    focus = {}
    for line in listing.stdout.splitlines():
        client, pane, style = (line.split("\t") + ["", ""])[:3]
        if pane and style != popup.DIM_STYLE:
            focus[client] = pane
    return focus


class FocusTracker:
    def __init__(self, dwell: float = DEFAULT_SEEN_DWELL_SECONDS):
        self.dwell = dwell
        self.since: dict[str, tuple[str, float]] = {}

    def observe(self, focus: dict[str, str], now: float) -> dict[str, float]:
        self.since = {
            client: (pane, self.since[client][1] if self.since.get(client, ("", 0))[0] == pane else now)
            for client, pane in focus.items()
        }
        return {pane: since for pane, since in self.since.values()}

    def seen(self, sessions: list[dict], focus_since: dict[str, float], now: float) -> list[str]:
        return [
            s["session_id"] for s in sessions
            if s.get("attention") == DONE and s.get("pane") in focus_since
            and now - max(focus_since[s["pane"]], s["finished"]) >= self.dwell
        ]


def matching_checkouts(project: dict, action: CustomAction) -> list[Path]:
    checkouts = read_checkouts(Path(project["path"]))
    if not action.match:
        return checkouts
    pattern = str(Path(action.match).expanduser())
    return [checkout for checkout in checkouts if fnmatch.fnmatch(str(checkout), pattern)]


def action_applies(project: dict, action: CustomAction) -> bool:
    return not action.match or bool(matching_checkouts(project, action))


def action_context(config: Config, project: dict, action: CustomAction) -> tuple[Path, dict[str, str]]:
    project_dir = Path(project["path"])
    checkouts = matching_checkouts(project, action)
    if action.match and not checkouts:
        raise RuntimeError(f"{project['name']} has no checkout matching {action.match}")
    if action.cwd == "project":
        cwd = project_dir
    elif checkouts:
        cwd = checkouts[0]
    else:
        raise RuntimeError(f"{project['name']} has no checkouts")
    env = {"GOPHER_ROOT": str(config.root), "GOPHER_PROJECT": project["name"], "GOPHER_PROJECT_DIR": str(project_dir)}
    return cwd, env


def title_text(project: dict) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    if stars := project.get("priority", 0):
        text.append(PRIORITY_ICON * stars + " ", style="bold yellow")
    text.append(project["name"], style="bold")
    text.append(f"  {updated_age(project.get('updated'))}", style="dim")
    return text


def project_state_text(project: dict) -> Text:
    state = project.get("state") or ""
    colour = PROJECT_STATE_COLOUR.get(state)
    return Text().append(state, style=colour or "dim")


def state_class(project: dict) -> str:
    state = project.get("state") or ""
    if project.get("window_open") is False:
        return "pstate-other"
    return f"pstate-{state}" if state in PROJECT_STATE_COLOUR else "pstate-other"


def facts_text(project: dict) -> Text:
    text = inbox_text(project["inbox"])
    links = links_text(project.get("links", {}))
    if text and links:
        text.append("  ")
    text.append_text(links)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def body_text(project: dict, cached: str | None) -> Text:
    if cached:
        return Text(f"~ {cached}", style="italic")
    return Text(project["summary"] or "(no summary)")


def detail_header(project: dict, cached: str | None) -> Text:
    text = title_text(project)
    text.append("  ")
    text.append_text(project_state_text(project))
    text.append(f"\n{project['summary'] or '(no summary)'}\n")
    if cached:
        text.append(f"~ {cached}\n", style="italic")
    for session in sorted(project["sessions"], key=lambda s: STATE_ORDER.get(effective_state(s), 3)):
        text.append("\nagent ", style="dim")
        text.append(f"{session.get('name') or '?'} ")
        text.append_text(state_text(effective_state(session) or "?"))
    for item in unread(Path(project["path"])):
        text.append(f"\n{INBOX_ICON} ", style="bold" if item.wake else "")
        text.append(f"{item.sender}, {item.created}: ", style="dim")
        text.append(item.body.splitlines()[0][:120] if item.body else "")
    for kind, items in project.get("links", {}).items():
        if items:
            text.append("\n")
            text.append_text(resource_list(kind, items))
    for checkout in project["checkouts"]:
        text.append(f"\ncheckout {checkout}", style="dim")
    text.append_text(todos_text(project, limit=None))
    return text


def status_body(project_dir: Path) -> str:
    path = project_dir / "STATUS.md"
    if not path.is_file():
        return "_No STATUS.md._"
    return read_frontmatter(path.read_text())[1].strip()


def home_relative(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def checkouts_text(project: dict) -> Text:
    text = Text()
    checkouts = project.get("checkouts") or []
    if not checkouts:
        return text
    cwds = [s.get("cwd") or "" for s in project["sessions"]]
    text.append("\ncheckouts", style="bold")
    for checkout in checkouts:
        text.append("\n  ")
        here = any(cwd == checkout or cwd.startswith(checkout.rstrip("/") + "/") for cwd in cwds)
        text.append("\u25b8 " if here else "  ", style="green")
        text.append(home_relative(checkout), style="" if Path(checkout).is_dir() else "dim strike")
        if not Path(checkout).is_dir():
            text.append("  missing", style="red")
    return text


PINNED_TODOS = 5


def todos_text(project: dict, limit: int | None = PINNED_TODOS) -> Text:
    text = Text()
    todos = project.get("todos") or []
    if not todos:
        return text
    text.append(f"\nto-do ({len(todos)})", style="bold")
    for item in todos[:limit]:
        text.append("\n  \u25a1 ", style="dim")
        text.append(item)
    if limit is not None and len(todos) > limit:
        text.append(f"\n  +{len(todos) - limit} more in plan/todo.md", style="dim")
    return text


def pinned_parts(project: dict, cached: str | None) -> dict[str, Text]:
    body = Text(project["summary"] or "(no summary)")
    if cached:
        body.append(f"\n~ {cached}", style="italic")
    for session in sorted(project["sessions"], key=lambda s: STATE_ORDER.get(effective_state(s), 3)):
        body.append("\n")
        body.append_text(state_text(effective_state(session) or "?"))
        body.append(f"  {session.get('name') or '?'}", style="dim")
    body.append_text(checkouts_text(project))
    body.append_text(todos_text(project))
    return {
        "name": title_text(project),
        "pstate": project_state_text(project),
        "facts": facts_text(project),
        "agents": agents_text(project["sessions"]),
        "body": body,
    }


def inbox_line(item) -> Text:
    text = Text()
    text.append(f"{INBOX_ICON}{'!' if item.wake else ' '} ", style="bold" if item.wake else "")
    text.append(f"{item.sender} {item.created[5:16].replace('T', ' ')}  ", style="dim")
    text.append(item.body.splitlines()[0] if item.body else "")
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


class ProjectCard(ListItem):
    def __init__(self, project: dict, cached: str | None, compact: bool = False, summary_lines: int = DEFAULT_SUMMARY_LINES):
        super().__init__(classes=f"card {'compact' if compact else ''} {state_class(project)}")
        self.project_name = project["name"]
        self.project = project
        self.cached = cached
        self.compact = compact
        self.summary_lines = summary_lines if compact else None
        if compact:
            self.styles.height = 2 + summary_lines

    def parts(self) -> dict[str, Text]:
        project = self.project
        parts = {
            "name": title_text(project),
            "pstate": project_state_text(project),
            "facts": facts_text(project),
            "agents": agents_text(project["sessions"]),
        }
        if self.summary_lines != 0:
            parts["body"] = body_text(project, self.cached)
        return parts

    def compose(self) -> ComposeResult:
        parts = self.parts()
        with Horizontal(classes="row"):
            yield Static(parts["name"], classes="name")
            yield Static(parts["pstate"], classes="pstate")
        with Horizontal(classes="row"):
            yield Static(parts["facts"], classes="facts")
            yield Static(parts["agents"], classes="agents")
        if "body" in parts:
            body = Static(parts["body"], classes="body")
            if self.summary_lines:
                body.styles.height = self.summary_lines
            yield body

    def update_card(self, project: dict, cached: str | None) -> None:
        self.project, self.cached = project, cached
        self.remove_class(*STATE_CLASSES).add_class(state_class(project))
        for name, text in self.parts().items():
            self.query_one(f".{name}", Static).update(text)


class InboxRow(ListItem):
    def __init__(self, item):
        super().__init__(Static(inbox_line(item)))
        self.item = item


class InboxList(ListView):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("x", "app.inbox_read", "Mark read"),
        Binding("l", "app.inbox_show", "Show"),
        Binding("escape,b", "app.toggle_inbox", "Close inbox"),
    ]

    def selected_item(self):
        row = self.highlighted_child
        return row.item if isinstance(row, InboxRow) else None


class SectionLabel(ListItem):
    def __init__(self, label: str):
        super().__init__(Static(Text(label, style="bold")), classes="section", disabled=True)
        self.label = label


def fuzzy_match(pattern: str, name: str) -> bool:
    pattern, name = pattern.lower(), name.lower()
    if not pattern:
        return True
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatch(name, pattern if pattern.startswith("*") else f"*{pattern}")
    letters = iter(name)
    return all(ch in letters for ch in pattern)


def sections(projects: list[dict]) -> list[str | dict]:
    starred = [p for p in projects if p.get("priority", 0)]
    rest = [p for p in projects if not p.get("priority", 0)]
    if not starred:
        return rest
    return ["Priority", *starred, *(["Projects", *rest] if rest else [])]


class ProjectList(ListView):
    BINDINGS = [Binding("up", "app.move(-1)", "Up", show=False)]


BUTTON_HINTS = {
    "toggle_inbox": "Read this project's unread inbox; enter hands an item to the agent beside you",
    "note": "Write a new inbox item for this project's agent (it waits until you say)",
    "refresh_status": "Ask the agent beside you to refresh STATUS.md now",
    "edit_human": "Edit your own notes, human/STATUS.md, in a popup",
    "quit_tab": "Save this window's layout and agent, then close it (o reopens it)",
    "more": "More: close this tab, archive the project, or close the project",
}
LIFECYCLE_HINTS = {
    "archive": "Park {name}: move it to archived/, keep checkouts, run archive cleanup",
    "close": "End {name}: cleanup, remove its git worktrees, move it to closed/",
}


def more_actions(name: str, pinned: bool) -> list[tuple[str, str, str, str]]:
    if pinned:
        window = ("Q", "Close tab", "quit_tab", BUTTON_HINTS["quit_tab"])
    else:
        window = ("c", "Close window", f"close_window({name!r})", f"Save {name}'s window layout and agent, then close it (o reopens it)")
    return [
        window,
        ("A", "Archive", f"lifecycle('archive', {name!r})", LIFECYCLE_HINTS["archive"].format(name=name)),
        ("X", "Close project", f"lifecycle('close', {name!r})", LIFECYCLE_HINTS["close"].format(name=name)),
    ]


class MoreScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape,q,h", "cancel", "Cancel"), Binding("j", "cursor(1)", show=False), Binding("k", "cursor(-1)", show=False)]

    def __init__(self, name: str, actions: list[tuple[str, str, str, str]]):
        super().__init__()
        self.project_name, self.actions = name, actions

    def compose(self) -> ComposeResult:
        options = []
        for key, label, action, hint in self.actions:
            prompt = Text(f"{key} {label}", style="bold")
            prompt.append(f"\n  {hint}", style="dim")
            options.append(Option(prompt, id=action))
        with Vertical(id="more-box"):
            yield Static(Text(f"{self.project_name} (enter runs, escape closes)", style="bold"))
            yield OptionList(*options, id="more-list")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event) -> None:
        for key, _, action, _ in self.actions:
            if event.character == key:
                event.stop()
                self.dismiss(action)

    def action_cursor(self, step: int) -> None:
        options = self.query_one(OptionList)
        options.action_cursor_down() if step > 0 else options.action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss(None)


def header_buttons(specs) -> list[Button]:
    buttons = []
    for label, action in specs:
        button = Button(label, action=f"app.{action}", compact=True)
        button.hint = BUTTON_HINTS[action]
        buttons.append(button)
    return buttons


def custom_hint(custom: CustomAction) -> str:
    return custom.description or f"Run `{custom.command}` in a popup"


class ButtonRow(HorizontalScroll, can_focus=False):
    pass


class HeaderButtons(Vertical):
    BINDINGS = [
        Binding("h,left", "cycle(-1)", "Prev", show=False),
        Binding("l,right", "cycle(1)", "Next", show=False),
        Binding("j,down", "projects", "Projects", show=False),
        Binding("k,up", "stay", "Stay", show=False),
    ]

    def action_cycle(self, step: int) -> None:
        buttons = [button for button in self.query(Button) if button.display]
        if not buttons:
            return
        current = buttons.index(self.screen.focused) if self.screen.focused in buttons else 0
        buttons[(current + step) % len(buttons)].focus()

    def action_projects(self) -> None:
        self.app.query_one("#projects", ListView).focus()

    def action_stay(self) -> None:
        pass


class PinnedHeader(Vertical):
    def __init__(self, actions: list[CustomAction]):
        super().__init__()
        self.actions = actions

    def compose(self) -> ComposeResult:
        with Horizontal(classes="row"):
            yield Static(classes="name")
            yield Static(classes="pstate")
        with Horizontal(classes="row"):
            yield Static(classes="facts")
            yield Static(classes="agents")
        yield Static(id="pinned-text", classes="body")
        with HeaderButtons(id="pinned-buttons"):
            with ButtonRow(classes="button-row"):
                for index, custom in enumerate(self.actions):
                    button = Button(f"{custom.key} {custom.label}", action=f"app.custom({index})", compact=True, id=f"custom-{index}")
                    button.hint = custom_hint(custom)
                    yield button
                yield from header_buttons((("b Inbox", "toggle_inbox"), ("n Note", "note"), ("u Refresh", "refresh_status"), ("e Human", "edit_human")))
                yield from header_buttons(((". More", "more"),))
        yield Static(id="button-hint")
        yield InboxList(id="pinned-inbox")

    def on_descendant_focus(self, event) -> None:
        hint = self.query_one("#button-hint", Static)
        if isinstance(event.widget, Button):
            hint.update(Text(getattr(event.widget, "hint", ""), style="dim italic"))
            hint.add_class("open")
        else:
            hint.remove_class("open")

    def on_descendant_blur(self, event) -> None:
        if not isinstance(self.screen.focused, Button) or self.screen.focused not in self.query(Button):
            self.query_one("#button-hint", Static).remove_class("open")


class DetailScreen(Screen):
    BINDINGS = [
        Binding("h,escape,left", "back", "Back"),
        Binding("j,down", "scroll(1)", "Down", show=False),
        Binding("k,up", "scroll(-1)", "Up", show=False),
        Binding("q", "back", "Back", show=False),
    ]

    def __init__(self, gopher: "GopherTUI", project: dict):
        super().__init__()
        self.gopher = gopher
        self.project = project
        self.timer = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(detail_header(self.project, self.gopher.cache.get(self.project)), id="header")
            yield Static("", id="rich")
            yield Markdown(status_body(Path(self.project["path"])))
        yield Footer()

    def on_mount(self) -> None:
        self.restart_timer()
        if detail := self.gopher.details.get(self.project):
            self.show_rich(detail)
        else:
            self.query_one("#rich", Static).update(Text("Summarizing with Haiku...", style="dim italic"))
            self.run_worker(self.load_rich, thread=True)

    def load_rich(self) -> None:
        try:
            detail = summarize(self.gopher.config, Path(self.project["path"]), "tui-detail", flatten=False)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.app.call_from_thread(self.show_rich, f"Haiku summary failed: {error}")
            return
        self.gopher.details.put(self.project["name"], detail)
        self.app.call_from_thread(self.show_rich, detail)

    def show_rich(self, detail: str) -> None:
        self.query_one("#rich", Static).update(Text(f"\n{detail}\n"))

    def restart_timer(self) -> None:
        if self.timer:
            self.timer.stop()
        self.timer = self.set_timer(DETAIL_TIMEOUT, self.action_back)

    def on_key(self) -> None:
        self.restart_timer()

    def action_scroll(self, lines: int) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=lines * 3)

    def action_back(self) -> None:
        if self.app.screen is self:
            self.dismiss()


class TextScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,h", "dismiss", "Close")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(self.text)

    def action_dismiss(self) -> None:
        self.dismiss(None)


class MessageScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, project: str, wake: bool):
        super().__init__()
        self.title_line = f"Inbox item for {project}{' (wake)' if wake else ''}: enter to file, escape to cancel"

    def compose(self) -> ComposeResult:
        yield Static(self.title_line)
        yield Input(placeholder="message")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.question)
            yield Static(Text("Type yes and press enter to confirm; anything else cancels.", style="dim"))
            yield Input(placeholder="yes", id="confirm-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class GopherTUI(App):
    TITLE = "gopher"
    CSS = """
    #projects { height: 1fr; }
    #bottom { dock: bottom; height: auto; }
    #bottom Footer { dock: none; }
    #bottom-rule { margin: 0; color: ansi_bright_black; }
    #backup { height: 1; padding: 0 1; }
    #health { height: auto; padding: 0 1; }
    ListItem.card { height: 5; margin-bottom: 1; padding: 0 1; border-left: thick ansi_bright_black; }
    ListItem.card.compact { height: 2; }
    ListItem.card.pstate-active { border-left: thick ansi_green; }
    ListItem.card.pstate-blocked { border-left: thick ansi_red; }
    ListItem.card.pstate-waiting-review { border-left: thick ansi_yellow; }
    ListItem.section { height: 1; padding: 0 1; }
    #search { display: none; height: 1; border: none; padding: 0 1; }
    #search.open { display: block; }
    ListItem.card .row { height: 1; }
    ListItem.card .name { width: 1fr; }
    ListItem.card .pstate { width: auto; }
    ListItem.card .facts { width: 1fr; padding-left: 2; }
    ListItem.card .agents { width: auto; padding-left: 1; }
    ListItem.card .body { height: 3; padding-left: 2; }
    PinnedHeader { height: auto; max-height: 60%; padding: 0 1; border-left: thick ansi_bright_black; }
    PinnedHeader.pstate-active { border-left: thick ansi_green; }
    PinnedHeader.pstate-blocked { border-left: thick ansi_red; }
    PinnedHeader.pstate-waiting-review { border-left: thick ansi_yellow; }
    #pinned-rule { margin: 0; color: ansi_bright_black; }
    PinnedHeader .row { height: 1; }
    PinnedHeader .name { width: 1fr; }
    PinnedHeader .pstate { width: auto; }
    PinnedHeader .facts { width: 1fr; padding-left: 2; }
    PinnedHeader .agents { width: auto; padding-left: 1; }
    PinnedHeader .body { height: auto; padding-left: 2; }
    #pinned-buttons { height: auto; margin: 1 0 0 0; }
    #pinned-buttons .button-row { height: 1; scrollbar-size-horizontal: 0; }
    #pinned-buttons Button { margin-right: 1; min-width: 0; width: auto; }
    #button-hint { display: none; height: auto; padding-left: 1; }
    #button-hint.open { display: block; }
    #pinned-inbox { height: auto; max-height: 10; display: none; }
    #pinned-inbox.open { display: block; }
    DetailScreen VerticalScroll { padding: 0 1; }
    MessageScreen, TextScreen, ConfirmScreen { align: center middle; }
    MoreScreen { align: center middle; }
    #more-box { width: 90%; height: auto; border: round ansi_bright_black; padding: 0 1; }
    #more-list { height: auto; border: none; }
    #confirm-box { width: 90%; height: auto; max-height: 90%; border: round ansi_yellow; padding: 0 1; }
    #confirm-box > Static { height: auto; }
    #confirm-input { border: none; height: 1; }
    MessageScreen > Static, MessageScreen > Input { width: 90%; }
    TextScreen > VerticalScroll { width: 90%; height: 80%; border: round ansi_bright_black; padding: 0 1; }
    """
    BINDINGS = [
        Binding("j", "move(1)", "Down", show=False),
        Binding("k", "move(-1)", "Up", show=False),
        Binding("home", "edge(0)", "Top", show=False),
        Binding("end", "edge(1)", "Bottom", show=False),
        Binding("l", "expand", "Expand"),
        Binding("t", "jump", "Jump", show=False),
        Binding("slash", "search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("o", "open_window", "Open"),
        Binding("plus", "priority(1)", "Priority+", show=False),
        Binding("minus", "priority(-1)", "Priority-", show=False),
        Binding("c", "close_window", "Close", show=False),
        Binding("g", "gopher", "Gopher"),
        Binding("i", "agent", "Agent"),
        Binding("m", "message(False)", "Message"),
        Binding("M", "message(True)", "Wake msg", show=False),
        Binding("x", "mark_seen", "Seen", show=False),
        Binding("r", "summarize", "Haiku line", show=False),
        Binding("b", "toggle_inbox", "Inbox", show=False),
        Binding("n", "note", "Note", show=False),
        Binding("u", "refresh_status", "Refresh", show=False),
        Binding("e", "edit_human", "Human", show=False),
        Binding("P", "proactivity", "Proactivity", show=False),
        Binding("D", "dismiss_problems", "Dismiss problems", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("q", "quit", "Quit"),
        Binding("full_stop", "more", "More", show=False),
        Binding("Q", "quit_tab", "Close tab", show=False),
    ]

    def __init__(self, config: Config, pinned: str | None = None):
        super().__init__(ansi_color=True)
        self.theme = "ansi-dark"
        self.config = config
        self.pinned = pinned
        self.cache = SummaryCache(config)
        self.details = SummaryCache(config, "details")
        self.acks = Acks(config)
        self.projects: dict[str, dict] = {}
        self.own_pane = os.environ.get("TMUX_PANE")
        self.actions: list[CustomAction] = []
        self.applicable: set[int] = set()
        self.search = ""
        self.ordered: list[dict] = []
        self.token = devreload.code_token()
        self.pending_reload: dict | None = None
        self.reload_stop: threading.Event | None = None
        self.last_input = time.time()
        try:
            self.idle_after = idle_seconds(config)
        except ConfigError as error:
            self.action_problems.append(str(error))
            self.idle_after = DEFAULT_IDLE_SECONDS
        self.reloading = False
        self.backup: BackupStatus | None = None
        self.heartbeat: tuple[str, bool] | None = None
        self.health: list[doctor.Check] = []
        self.gopher_state: str | None = None
        self.save_hours: float = 0
        self.backup_checked = 0.0
        self.action_problems: list[str] = []
        try:
            self.summary_lines = sidebar_summary_lines(config)
        except ConfigError as error:
            self.action_problems.append(str(error))
            self.summary_lines = DEFAULT_SUMMARY_LINES
        try:
            self.focus = FocusTracker(seen_dwell_seconds(config))
        except ConfigError as error:
            self.action_problems.append(str(error))
            self.focus = FocusTracker()
        if pinned:
            self.load_custom_actions()

    def load_custom_actions(self) -> None:
        try:
            configured = tui_actions(self.config)
        except ConfigError as error:
            self.action_problems.append(str(error))
            return
        taken = {key.strip() for binding in self.BINDINGS for key in binding.key.split(",")}
        for custom in configured:
            if custom.key in taken:
                self.action_problems.append(f"[[tui.actions]] {custom.label}: key {custom.key!r} is already taken")
                continue
            taken.add(custom.key)
            self.bind(custom.key, f"custom({len(self.actions)})", description=custom.label, show=False)
            self.actions.append(custom)

    def compose(self) -> ComposeResult:
        if self.pinned:
            yield PinnedHeader(self.actions)
            yield Rule(id="pinned-rule")
        yield ProjectList(id="projects")
        yield Input(placeholder="/ filter projects: letters in order, or a glob", id="search")
        with Vertical(id="bottom"):
            yield Footer()
            yield Rule(id="bottom-rule")
            yield Static(id="backup")
            yield Static(id="health")

    def on_mount(self) -> None:
        for problem in self.action_problems:
            self.notify(problem, severity="warning")
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        self.set_interval(RELOAD_CHECK_SECONDS, self.refocus_when_idle)
        self.start_dev_reload()

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, (events.Key, events.MouseDown, events.MouseScrollDown, events.MouseScrollUp)):
            self.last_input = time.time()
        await super().on_event(event)

    def refocus_when_idle(self) -> None:
        if not self.pinned or not self.idle_after or time.time() - self.last_input < self.idle_after:
            return
        if len(self.screen_stack) > 1 or isinstance(self.focused, Input) or self.query("#pinned-inbox.open"):
            return
        view = self.project_list()
        first = self.first_card_index()
        if isinstance(self.focused, Button) and view.index == first and view.scroll_y == 0:
            return
        view.index = first
        view.scroll_home(animate=False)
        self.focus_header()

    def start_dev_reload(self) -> None:
        if not devreload.enabled(self.config):
            return
        if not devreload.watcher_available():
            self.notify("[dev] reload is on but watchfiles is missing; install the dev extra", severity="warning")
            return
        self.reload_stop = threading.Event()
        self.run_worker(lambda: devreload.watch(self.config, self.reload_signalled, self.reload_stop), thread=True, group="dev-reload")
        self.set_interval(RELOAD_CHECK_SECONDS, self.reload_if_pending)

    def reload_signalled(self, signal: dict) -> None:
        token = signal.get("token")
        if token and token != self.token:
            self.pending_reload = signal

    def reload_if_pending(self) -> None:
        if not self.pending_reload or len(self.screen_stack) > 1 or isinstance(self.focused, Input):
            return
        self.reloading = True
        if self.reload_stop:
            self.reload_stop.set()
        self.exit()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "clear_search":
            return bool(self.search) or bool(self.query("#search.open"))
        if action == "custom":
            return bool(self.pinned) and bool(parameters) and parameters[0] in self.applicable
        if action in PINNED_ACTIONS:
            return bool(self.pinned)
        return True

    def project_list(self) -> ListView:
        return self.query_one("#projects", ListView)

    def cards(self) -> list[ProjectCard]:
        return list(self.project_list().query(ProjectCard))

    def selected(self) -> dict | None:
        card = self.project_list().highlighted_child
        return self.projects.get(card.project_name) if isinstance(card, ProjectCard) else None

    def pinned_project(self) -> dict | None:
        return self.projects.get(self.pinned) if self.pinned else None

    def action_refresh(self) -> None:
        self.run_worker(self.load, thread=True, exclusive=True, group="refresh")

    def load(self) -> None:
        projects = mark_open_windows(project_overview(self.config), open_window_names())
        now = time.time()
        focus_since = self.focus.observe(focused_panes(), now)
        seen = self.focus.seen([s for p in projects for s in p["sessions"]], focus_since, now)
        if seen:
            self.acks.ack(seen, now)
            projects = mark_open_windows(project_overview(self.config), open_window_names())
        if now - self.backup_checked >= BACKUP_REFRESH_SECONDS:
            self.backup, self.backup_checked = backup_status(self.config.root), now
            self.save_hours = schedule.save_interval_hours(self.config)
            self.health = doctor.undismissed(self.config, doctor.run(self.config, now=now))
            try:
                healed = heal_schedule(self.config)
            except (OSError, subprocess.SubprocessError):
                healed = None
            if healed:
                self.call_from_thread(self.notify, f"Schedule: {healed}")
        self.heartbeat = schedule.heartbeat_compact(self.config, now)
        self.gopher_state = gopher_state(self.config)
        self.call_from_thread(self.show, projects)

    async def show(self, projects: list[dict]) -> None:
        self.ordered = projects
        self.projects = {p["name"]: p for p in projects}
        self.show_backup()
        if self.pinned:
            self.show_pinned()
        rows = sections([p for p in projects if p["name"] != self.pinned and fuzzy_match(self.search, p["name"])])
        view = self.project_list()
        current = self.selected()
        shape = [row if isinstance(row, str) else row["name"] for row in rows]
        existing = [node.label if isinstance(node, SectionLabel) else node.project_name for node in view.children]
        if shape == existing:
            for card, project in zip(self.cards(), [row for row in rows if not isinstance(row, str)]):
                card.update_card(project, self.cache.get(project))
            return
        await view.clear()
        await view.extend(
            SectionLabel(row) if isinstance(row, str)
            else ProjectCard(row, self.cache.get(row), compact=bool(self.pinned), summary_lines=self.summary_lines)
            for row in rows
        )
        names = [None if isinstance(row, str) else row["name"] for row in rows]
        if current and current["name"] in names:
            view.index = names.index(current["name"])
        else:
            view.index = self.first_card_index()

    def show_backup(self) -> None:
        if not self.backup:
            return
        now = time.time()
        base = BACKUP_STYLES[self.backup.level(now, self.save_hours)]
        line = Text()
        for text, kind in self.backup.segments(now, self.save_hours):
            line.append(text, style=CHANGE_STYLES.get(kind, base))
        if self.heartbeat:
            text, stalled = self.heartbeat
            line.append("  ")
            line.append(text, style="yellow" if stalled else "bright_black")
        line.truncate(max(self.size.width - 2, 1), overflow="ellipsis")
        self.query_one("#backup", Static).update(line)
        self.query_one("#health", Static).update(health_text(self.gopher_state, self.health, max(self.size.width - 2, 1)))

    def action_dismiss_problems(self) -> None:
        if not self.health:
            self.notify("no problems to dismiss")
            return
        count = doctor.dismiss(self.config, self.health)
        self.health = []
        self.show_backup()
        self.notify(f"dismissed {count} problem(s); they come back if they change")

    def show_pinned(self) -> None:
        project = self.pinned_project()
        if not project:
            self.query_one("#pinned-text", Static).update(Text(f"{self.pinned}: no such project", style="red"))
            return
        header = self.query_one(PinnedHeader)
        header.remove_class(*STATE_CLASSES).add_class(state_class({**project, "window_open": None}))
        for name, text in pinned_parts(project, self.cache.get(project)).items():
            header.query_one(f".{name}", Static).update(text)
        self.applicable = {index for index, custom in enumerate(self.actions) if action_applies(project, custom)}
        for index in range(len(self.actions)):
            self.query_one(f"#custom-{index}", Button).display = index in self.applicable
        inbox = self.query_one(InboxList)
        if inbox.has_class("open"):
            self.fill_inbox()

    def fill_inbox(self) -> None:
        inbox = self.query_one(InboxList)
        items = unread(Path(self.pinned_project()["path"]))
        names = [row.item.path.name for row in inbox.query(InboxRow)]
        if names == [item.path.name for item in items]:
            return
        index = inbox.index
        inbox.clear()
        inbox.extend(InboxRow(item) for item in items)
        if items:
            inbox.index = min(index or 0, len(items) - 1)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, InboxRow):
            self.work_on_item(event.item.item)
        else:
            self.action_jump()

    def action_move(self, step: int) -> None:
        view = self.project_list()
        if step < 0 and (view.index is None or view.index <= (self.first_card_index() or 0)) and self.focus_header():
            return
        if step > 0:
            view.action_cursor_down()
        else:
            view.action_cursor_up()

    def focus_header(self) -> bool:
        if not self.pinned:
            return False
        buttons = [button for button in self.query("#pinned-buttons Button") if button.display]
        if buttons:
            buttons[0].focus()
        return bool(buttons)

    def search_box(self) -> Input:
        return self.query_one("#search", Input)

    def action_search(self) -> None:
        box = self.search_box()
        box.add_class("open")
        box.focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.search = event.value
            await self.show(self.ordered)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            if not event.value:
                event.input.remove_class("open")
            self.project_list().focus()

    async def action_clear_search(self) -> None:
        box = self.search_box()
        box.value = ""
        box.remove_class("open")
        self.search = ""
        await self.show(self.ordered)
        self.project_list().focus()

    def first_card_index(self) -> int | None:
        return next((i for i, node in enumerate(self.project_list().children) if isinstance(node, ProjectCard)), None)

    def action_edge(self, end: int) -> None:
        view = self.project_list()
        if len(view):
            view.index = len(view) - 1 if end else self.first_card_index()

    def target_project(self) -> dict | None:
        return self.selected() if self.focused is self.project_list() or not self.pinned else self.pinned_project()

    def action_priority(self, step: int) -> None:
        project = self.target_project()
        if not project:
            return
        priority = write_priority(Path(project["path"]), project.get("priority", 0) + step)
        self.notify(f"{project['name']}: priority {priority}")
        self.action_refresh()

    def action_expand(self) -> None:
        if project := self.selected():
            self.push_screen(DetailScreen(self, project))

    def action_jump(self) -> None:
        project = self.selected()
        if not project:
            return
        pane = jump_target(project["sessions"])
        if pane:
            self.jump_to(pane)
            return
        windows = [w for w, name in project_window_ids(project["name"])]
        if windows:
            self.jump_to(windows[0])
        else:
            self.notify(f"{project['name']} has no open window; o opens it", severity="warning")

    def jump_to(self, target: str) -> None:
        popup.jump_to(target)
        if self.search or self.query("#search.open"):
            self.call_later(self.action_clear_search)

    def own_session(self) -> str | None:
        if not self.own_pane:
            return None
        return tmux("display-message", "-p", "-t", self.own_pane, "#{session_name}").stdout.strip() or None

    def action_open_window(self) -> None:
        project = self.selected()
        if not project:
            return
        windows = [w for w, _ in project_window_ids(project["name"])]
        if windows:
            self.jump_to(windows[0])
            return
        self.notify(f"Opening {project['name']}...")
        self.run_worker(lambda: self.open_window(project["name"]), thread=True, group="window")

    def open_window(self, name: str) -> None:
        try:
            window, snapshot = restore.open_window(self.config, name, self.own_session())
        except restore.RestoreError as error:
            self.call_from_thread(self.notify, f"open {name}: {error}", severity="error")
            return
        source = "fresh layout" if snapshot.source == "fresh" else "restored"
        self.call_from_thread(self.notify, f"{name}: window opened ({source})")
        self.call_from_thread(self.jump_to, window)

    def action_close_window(self, name: str | None = None) -> None:
        project = self.projects.get(name) if name else self.target_project()
        if not project:
            return
        name = project["name"]
        if not project_window_ids(name):
            self.notify(f"{name} has no open window", severity="warning")
            return

        def answered(yes: bool | None) -> None:
            if yes:
                self.notify(f"Snapshotting and closing {name}...")
                self.run_worker(lambda: self.close_window(name), thread=True, group="window")

        self.push_screen(ConfirmScreen(f"Close {name}'s window? Its layout and agent session are saved for o to restore."), answered)

    def action_more(self) -> None:
        project = self.target_project()
        if not project:
            return
        name = project["name"]

        def chosen(action: str | None) -> None:
            if action:
                self.call_later(self.run_action, action)

        self.push_screen(MoreScreen(name, more_actions(name, name == self.pinned)), chosen)

    def action_lifecycle(self, action: str, name: str | None = None) -> None:
        project = self.projects.get(name) if name else self.target_project()
        if not project:
            return
        self.notify(f"Planning {action} of {project['name']}...")
        self.run_worker(lambda: self.plan_lifecycle(action, project["name"]), thread=True, group="lifecycle")

    def plan_lifecycle(self, action: str, name: str) -> None:
        try:
            plan = lifecycle.make_plan(self.config, action, name)
        except lifecycle.LifecycleError as error:
            self.call_from_thread(self.notify, f"{action} {name}: {error}", severity="error")
            return
        self.call_from_thread(self.confirm_lifecycle, plan)

    def confirm_lifecycle(self, plan: lifecycle.Plan) -> None:
        if plan.blockers:
            self.push_screen(TextScreen(lifecycle.describe(plan)))
            return

        def answered(yes: bool | None) -> None:
            if yes:
                self.notify(f"{plan.action.capitalize()} {plan.project}: running...")
                self.run_worker(lambda: self.run_lifecycle(plan), thread=True, group="lifecycle")
            else:
                self.notify("Nothing changed")

        self.push_screen(ConfirmScreen(lifecycle.describe(plan)), answered)

    def run_lifecycle(self, plan: lifecycle.Plan) -> None:
        try:
            report = lifecycle.execute(self.config, plan)
        except (lifecycle.LifecycleError, OSError) as error:
            self.call_from_thread(self.notify, f"{plan.action} {plan.project}: {error}", severity="error")
            return
        self.call_from_thread(self.notify, f"{plan.project}: " + "; ".join(report))
        self.call_from_thread(self.action_refresh)

    def action_quit_tab(self) -> None:
        project = self.pinned_project()
        if not project:
            return
        name = project["name"]

        def answered(yes: bool | None) -> None:
            if yes:
                self.notify(f"Snapshotting and closing this tab ({name})...")
                self.run_worker(lambda: self.close_window(name), thread=True, group="window")

        self.push_screen(ConfirmScreen(f"Close this tab ({name})? Its layout and agent session are saved for o to restore."), answered)

    def close_window(self, name: str) -> None:
        try:
            restore.close_window(self.config, name)
        except restore.RestoreError as error:
            self.call_from_thread(self.notify, f"close {name}: {error}", severity="error")
            return
        self.call_from_thread(self.notify, f"{name}: window closed (o restores it)")

    def open_popup(self, kind: str, target: str | None, prefill: str) -> None:
        if not self.own_pane:
            self.notify("not running in tmux", severity="warning")
            return
        try:
            popup.open_popup(kind, self.own_pane, target=target, prefill=prefill)
        except RuntimeError as error:
            self.notify(str(error), severity="warning")

    def action_gopher(self) -> None:
        project = self.target_project()
        self.open_popup("gopher", None, f"[project: {project['name']}] " if project else "")

    def action_agent(self) -> None:
        project = self.selected()
        if not project:
            return
        pane = jump_target(project["sessions"])
        if not pane:
            self.action_message(False)
            return
        origin = f"[from: {self.pinned}] " if self.pinned else ""
        self.open_popup("agent", pane, origin)

    def action_proactivity(self) -> None:
        level = next_level(gopher_proactivity(self.config))
        set_gopher_setting(self.config.root, "proactivity", level)
        self.config = load_config(self.config.root)
        write_prompt(self.config)
        pane = popup.gopher_agent_pane()
        if pane:
            tmux("send-keys", "-t", pane, "-l", f"Proactivity is now {level}; follow the {level} rules from `gopher prompt gopher`.")
            tmux("send-keys", "-t", pane, "Enter")
            self.notify(f"Proactivity: {level} (told Gopher)")
        else:
            self.notify(f"Proactivity: {level} (Gopher not running; applies at next launch)")

    def action_mark_seen(self) -> None:
        if project := self.target_project():
            done = [s["session_id"] for s in project["sessions"] if s.get("attention") == DONE]
            if done:
                self.acks.ack(done)
                self.action_refresh()

    def action_message(self, wake: bool, project: dict | None = None) -> None:
        project = project or self.selected()
        if not project:
            return

        def file(text: str | None) -> None:
            if text and text.strip():
                path = add_item(Path(project["path"]), text, sender="user", wake=wake)
                self.notify(f"Filed {path.name}")
                self.action_refresh()

        self.push_screen(MessageScreen(project["name"], wake), file)

    def action_note(self) -> None:
        if project := self.pinned_project():
            self.action_message(False, project)

    def action_toggle_inbox(self) -> None:
        if not self.pinned_project():
            return
        inbox = self.query_one(InboxList)
        if inbox.has_class("open"):
            inbox.remove_class("open")
            self.project_list().focus()
            return
        self.fill_inbox()
        inbox.add_class("open")
        inbox.focus()

    def inbox_item(self):
        return self.query_one(InboxList).selected_item()

    def work_on_item(self, item) -> None:
        pane = agent_pane_beside(self.own_pane)
        if not pane:
            self.notify("no agent pane in this window", severity="warning")
            return
        tmux("send-keys", "-t", pane, "-l", f"Inbox item inbox/{item.path.name}: ")
        tmux("select-pane", "-t", pane)

    def action_inbox_read(self) -> None:
        if item := self.inbox_item():
            mark_read(item.path)
            self.fill_inbox()
            self.action_refresh()

    def action_inbox_show(self) -> None:
        if item := self.inbox_item():
            self.push_screen(TextScreen(f"{item.sender}, {item.created}{' (wake)' if item.wake else ''}\n\n{item.body}"))

    def action_custom(self, index: int) -> None:
        project = self.pinned_project()
        if not project or index not in self.applicable or not self.own_pane:
            return
        custom = self.actions[index]
        try:
            cwd, env = action_context(self.config, project, custom)
            popup.command_popup(self.own_pane, cwd, custom.command, env, title=f" {custom.label}: {project['name']} ")
        except RuntimeError as error:
            self.notify(f"{custom.label}: {error}", severity="warning")

    def action_refresh_status(self) -> None:
        pane = agent_pane_beside(self.own_pane)
        if not pane:
            self.notify("no agent pane in this window", severity="warning")
            return
        tmux("send-keys", "-t", pane, "-l", REFRESH_STATUS_PROMPT)
        tmux("send-keys", "-t", pane, "Enter")
        self.notify("Asked the agent to refresh STATUS.md")

    def action_edit_human(self) -> None:
        project = self.pinned_project()
        if not project or not self.own_pane:
            return
        project_dir = Path(project["path"])
        (project_dir / "human").mkdir(exist_ok=True)
        command = shlex.join(status_editor_command(project_dir))
        subprocess.Popen(
            ["tmux", "display-popup", "-E", "-t", self.own_pane, "-d", str(project_dir), "-w", "80%", "-h", "80%", command],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )

    def action_summarize(self) -> None:
        if project := self.target_project():
            self.notify(f"Summarizing {project['name']}...")
            self.run_worker(lambda: self.refresh_summary(project), thread=True, group="summary")

    def refresh_summary(self, project: dict) -> None:
        try:
            summary = summarize(self.config, Path(project["path"]))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.call_from_thread(self.notify, f"Summary failed: {error}", severity="error")
            return
        self.cache.put(project["name"], summary)
        self.call_from_thread(self.action_refresh)


def open_window_names() -> set[str] | None:
    listing = tmux("list-windows", "-a", "-F", "#{window_name}")
    if listing.returncode != 0:
        return None
    return {LEADING_GLYPHS.sub("", name) for name in listing.stdout.splitlines()}


def mark_open_windows(projects: list[dict], names: set[str] | None) -> list[dict]:
    for project in projects:
        project["window_open"] = None if names is None else project["name"] in names
    return projects


def project_window_ids(project: str) -> list[tuple[str, str]]:
    listing = tmux("list-windows", "-a", "-F", "#{window_id}\t#{window_name}")
    if listing.returncode != 0:
        return []
    found = []
    for line in listing.stdout.splitlines():
        window, _, name = line.partition("\t")
        if LEADING_GLYPHS.sub("", name) == project:
            found.append((window, project))
    return found


def detect_pinned(config: Config) -> str | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    projects = {p["name"]: p for p in project_overview(config)}
    return pane_project(config, pane, projects)


def run(config: Config, project: str | None = None) -> None:
    app = GopherTUI(config, pinned=project or detect_pinned(config))
    app.run()
    if app.reloading:
        os.environ[RELOAD_ENV] = str(int(os.environ.get(RELOAD_ENV, "0")) + 1)
        os.execv(sys.executable, [sys.executable, *sys.argv])
