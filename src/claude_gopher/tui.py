import datetime
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, ListItem, ListView, Markdown, Static

from claude_gopher.config import (
    PROACTIVITY_LEVELS, Config, ConfigError, CustomAction, gopher_proactivity, load_config, set_gopher_setting,
    tui_actions,
)
from claude_gopher import popup
from claude_gopher.checkouts import read_checkouts
from claude_gopher.inbox import add_item, mark_read, unread
from claude_gopher.launch import write_prompt
from claude_gopher.overview import (
    CREDS, DONE, Acks, Resource, effective_state, most_urgent, open_tickets, project_overview,
    visible_prs,
)
from claude_gopher.projects import read_frontmatter
from claude_gopher.prompt import assemble
from claude_gopher.registry import BACKGROUND
from claude_gopher.state import IDLE, LEADING_GLYPHS, WAITING, WORKING, resolve_project
from claude_gopher.workon import status_editor_command

REFRESH_SECONDS = 5
DWELL_SECONDS = 5
DETAIL_TIMEOUT = 90
SUMMARY_MODEL = "haiku"
SUMMARY_TIMEOUT = 120
ACTION_TIMEOUT = 60
PINNED_ACTIONS = ("custom", "toggle_inbox", "note", "refresh_status", "edit_human")
REFRESH_STATUS_PROMPT = "Please refresh STATUS.md (frontmatter and body) so it reflects where this project stands now."
STATE_ORDER = {WAITING: 0, CREDS: 0, DONE: 0, WORKING: 1, BACKGROUND: 1, IDLE: 2}
STATE_STYLE = {
    WAITING: "bold yellow", CREDS: "bold red", DONE: "bold green", WORKING: "green", BACKGROUND: "green", IDLE: "dim",
}
STATE_LABEL = {WAITING: "needs input", CREDS: "needs login"}
STATE_ICON = {
    WAITING: "\u25cf", CREDS: "\u2715", DONE: "\u2714", WORKING: "\u2605", BACKGROUND: "\u25d0", IDLE: "\u25cb",
}
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
    for pr, count, merged in visible_prs(links.get("prs", []), now):
        if text:
            text.append(" ")
        if merged:
            text.append(f"{DONE_ICON}", style="green")
        text.append_text(link(f"#{pr.name}", pr.url))
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
    listing = tmux("list-clients", "-F", "#{client_name}\t#{pane_id}")
    if listing.returncode != 0:
        return {}
    return dict(line.split("\t", 1) for line in listing.stdout.splitlines() if "\t" in line)


class FocusTracker:
    def __init__(self, dwell: float = DWELL_SECONDS):
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


def run_custom_action(config: Config, project: dict, action: CustomAction) -> str:
    project_dir = Path(project["path"])
    if action.cwd == "project":
        cwd = project_dir
    else:
        checkouts = read_checkouts(project_dir)
        if not checkouts:
            raise RuntimeError(f"{project['name']} has no checkouts")
        cwd = checkouts[0]
    env = {**os.environ, "GOPHER_ROOT": str(config.root), "GOPHER_PROJECT": project["name"], "GOPHER_PROJECT_DIR": str(project_dir)}
    result = subprocess.run(action.command, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=ACTION_TIMEOUT)
    output = (result.stdout + result.stderr).strip()
    return output or f"(no output, exit {result.returncode})"


def title_text(project: dict) -> Text:
    text = Text(project["name"], style="bold", no_wrap=True, overflow="ellipsis")
    text.append(f"  {updated_age(project.get('updated'))}", style="dim")
    return text


def project_state_text(project: dict) -> Text:
    state = project.get("state") or ""
    colour = PROJECT_STATE_COLOUR.get(state)
    return Text().append(state, style=colour or "dim")


def state_class(project: dict) -> str:
    state = project.get("state") or ""
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
    return text


def status_body(project_dir: Path) -> str:
    path = project_dir / "STATUS.md"
    if not path.is_file():
        return "_No STATUS.md._"
    return read_frontmatter(path.read_text())[1].strip()


def pinned_text(project: dict, cached: str | None) -> Text:
    text = title_text(project)
    text.append("  ")
    text.append_text(project_state_text(project))
    text.append(f"\n{project['summary'] or '(no summary)'}")
    if cached:
        text.append(f"\n~ {cached}", style="italic")
    for session in sorted(project["sessions"], key=lambda s: STATE_ORDER.get(effective_state(s), 3)):
        text.append("\n")
        text.append_text(state_text(effective_state(session) or "?"))
        text.append(f"  {session.get('name') or '?'}", style="dim")
    facts = facts_text(project)
    if facts:
        text.append("\n")
        text.append_text(facts)
    return text


def inbox_line(item) -> Text:
    text = Text()
    text.append(f"{INBOX_ICON}{'!' if item.wake else ' '} ", style="bold" if item.wake else "")
    text.append(f"{item.sender} {item.created[5:16].replace('T', ' ')}  ", style="dim")
    text.append(item.body.splitlines()[0] if item.body else "")
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


class ProjectCard(ListItem):
    def __init__(self, project: dict, cached: str | None, compact: bool = False):
        super().__init__(classes=f"card {'compact' if compact else ''} {state_class(project)}")
        self.project_name = project["name"]
        self.project = project
        self.cached = cached
        self.compact = compact

    def parts(self) -> dict[str, Text]:
        project = self.project
        parts = {
            "name": title_text(project),
            "pstate": project_state_text(project),
            "facts": facts_text(project),
            "agents": agents_text(project["sessions"]),
        }
        if not self.compact:
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
        if not self.compact:
            yield Static(parts["body"], classes="body")

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


class PinnedHeader(Vertical):
    def __init__(self, actions: list[CustomAction]):
        super().__init__()
        self.actions = actions

    def compose(self) -> ComposeResult:
        yield Static(id="pinned-text")
        with Horizontal(id="pinned-buttons"):
            for index, custom in enumerate(self.actions):
                yield Button(f"{custom.key} {custom.label}", action=f"app.custom({index})", compact=True)
            yield Button("b Inbox", action="app.toggle_inbox", compact=True)
            yield Button("n Note", action="app.note", compact=True)
            yield Button("u Refresh", action="app.refresh_status", compact=True)
            yield Button("e Human", action="app.edit_human", compact=True)
        yield InboxList(id="pinned-inbox")
        yield Static(id="pinned-output")


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


class GopherTUI(App):
    TITLE = "gopher"
    CSS = """
    #projects { height: 1fr; }
    ListItem.card { height: 5; margin-bottom: 1; padding: 0 1; border-left: thick ansi_bright_black; }
    ListItem.card.compact { height: 2; }
    ListItem.card.pstate-active { border-left: thick ansi_green; }
    ListItem.card.pstate-blocked { border-left: thick ansi_red; }
    ListItem.card.pstate-waiting-review { border-left: thick ansi_yellow; }
    ListItem.card .row { height: 1; }
    ListItem.card .name { width: 1fr; }
    ListItem.card .pstate { width: auto; }
    ListItem.card .facts { width: 1fr; padding-left: 2; }
    ListItem.card .agents { width: auto; padding-left: 1; }
    ListItem.card .body { height: 3; padding-left: 2; }
    PinnedHeader { height: auto; max-height: 60%; padding: 0 1; border-bottom: solid ansi_bright_black; }
    #pinned-buttons { height: 1; margin: 1 0 0 0; }
    #pinned-buttons Button { margin-right: 1; }
    #pinned-inbox { height: auto; max-height: 10; display: none; }
    #pinned-inbox.open { display: block; }
    #pinned-output { height: auto; max-height: 15; display: none; }
    #pinned-output.open { display: block; }
    DetailScreen VerticalScroll { padding: 0 1; }
    MessageScreen, TextScreen { align: center middle; }
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
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, pinned: str | None = None):
        super().__init__(ansi_color=True)
        self.theme = "ansi-dark"
        self.config = config
        self.pinned = pinned
        self.cache = SummaryCache(config)
        self.details = SummaryCache(config, "details")
        self.acks = Acks(config)
        self.focus = FocusTracker()
        self.projects: dict[str, dict] = {}
        self.own_pane = os.environ.get("TMUX_PANE")
        self.actions: list[CustomAction] = []
        self.action_problems: list[str] = []
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
        yield ListView(id="projects")
        yield Footer()

    def on_mount(self) -> None:
        for problem in self.action_problems:
            self.notify(problem, severity="warning")
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
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
        projects = project_overview(self.config)
        now = time.time()
        focus_since = self.focus.observe(focused_panes(), now)
        seen = self.focus.seen([s for p in projects for s in p["sessions"]], focus_since, now)
        if seen:
            self.acks.ack(seen, now)
            projects = project_overview(self.config)
        self.call_from_thread(self.show, projects)

    async def show(self, projects: list[dict]) -> None:
        self.projects = {p["name"]: p for p in projects}
        if self.pinned:
            self.show_pinned()
        listed = [p for p in projects if p["name"] != self.pinned]
        view = self.project_list()
        current = self.selected()
        cards = self.cards()
        if [card.project_name for card in cards] == [p["name"] for p in listed]:
            for card, project in zip(cards, listed):
                card.update_card(project, self.cache.get(project))
            return
        await view.clear()
        await view.extend(ProjectCard(p, self.cache.get(p), compact=bool(self.pinned)) for p in listed)
        names = [p["name"] for p in listed]
        view.index = names.index(current["name"]) if current and current["name"] in names else (0 if names else None)

    def show_pinned(self) -> None:
        project = self.pinned_project()
        if not project:
            self.query_one("#pinned-text", Static).update(Text(f"{self.pinned}: no such project", style="red"))
            return
        self.query_one("#pinned-text", Static).update(pinned_text(project, self.cache.get(project)))
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
        if step > 0:
            view.action_cursor_down()
        else:
            view.action_cursor_up()

    def action_edge(self, end: int) -> None:
        view = self.project_list()
        if len(view):
            view.index = len(view) - 1 if end else 0

    def action_expand(self) -> None:
        if project := self.selected():
            self.push_screen(DetailScreen(self, project))

    def action_jump(self) -> None:
        project = self.selected()
        if not project:
            return
        pane = jump_target(project["sessions"])
        if pane:
            popup.jump_to(pane)
            return
        windows = [w for w, name in project_window_ids(project["name"])]
        if windows:
            popup.jump_to(windows[0])
        else:
            self.notify(f"{project['name']} has no open window", severity="warning")

    def open_popup(self, kind: str, target: str | None, prefill: str) -> None:
        if not self.own_pane:
            self.notify("not running in tmux", severity="warning")
            return
        try:
            popup.open_popup(kind, self.own_pane, target=target, prefill=prefill)
        except RuntimeError as error:
            self.notify(str(error), severity="warning")

    def action_gopher(self) -> None:
        project = self.selected() or self.pinned_project()
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
        if project := self.selected() or self.pinned_project():
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
        if not project or not 0 <= index < len(self.actions):
            return
        custom = self.actions[index]
        output = self.query_one("#pinned-output", Static)
        output.update(Text(f"Running {custom.label}...", style="dim italic"))
        output.add_class("open")
        self.run_worker(lambda: self.load_custom(project, custom), thread=True, exclusive=True, group="custom")

    def load_custom(self, project: dict, custom: CustomAction) -> None:
        try:
            text = run_custom_action(self.config, project, custom)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            text = f"{custom.label} failed: {error}"
        self.call_from_thread(self.query_one("#pinned-output", Static).update, Text(text))

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
        if project := self.selected() or self.pinned_project():
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
    GopherTUI(config, pinned=project or detect_pinned(config)).run()
