import datetime
import json
import os
import subprocess
import time
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, ListItem, ListView, Markdown, Static

from claude_gopher.config import PROACTIVITY_LEVELS, Config, gopher_proactivity, load_config, set_gopher_setting
from claude_gopher.inbox import add_item, unread
from claude_gopher.launch import write_prompt
from claude_gopher.overview import (
    CREDS, DONE, Acks, Resource, effective_state, most_urgent, open_tickets, project_overview,
    visible_prs,
)
from claude_gopher.projects import read_frontmatter
from claude_gopher.prompt import assemble
from claude_gopher.registry import BACKGROUND
from claude_gopher.state import IDLE, LEADING_GLYPHS, WAITING, WORKING, resolve_project

REFRESH_SECONDS = 5
DWELL_SECONDS = 5
DETAIL_TIMEOUT = 90
SUMMARY_MODEL = "haiku"
SUMMARY_TIMEOUT = 120
GOPHER_SESSION = "gopher"
POPUP_CLIENT_OPTION = "@gopher_popup_client"
POPUP_PANE_OPTION = "@gopher_popup_pane"
POPUP_POLL_SECONDS = 0.5
SHELLS = ("watch", "zsh", "bash", "sh", "fish")
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


def gopher_pane(own_pane: str | None) -> str | None:
    if not own_pane:
        return None
    listing = tmux("list-panes", "-t", own_pane, "-F", "#{pane_id}\t#{pane_current_command}")
    if listing.returncode != 0:
        return None
    others = [line.split("\t") for line in listing.stdout.splitlines() if not line.startswith(f"{own_pane}\t")]
    for pane, command in others:
        if command not in SHELLS:
            return pane
    return others[0][0] if others else None


def popup_clients() -> tuple[str | None, list[str]]:
    outer = tmux("show", "-gv", POPUP_CLIENT_OPTION)
    listing = tmux("list-clients", "-F", "#{client_name}\t#{client_session}")
    if outer.returncode != 0 or not outer.stdout.strip() or listing.returncode != 0:
        return None, []
    nested = [line.split("\t")[0] for line in listing.stdout.splitlines() if line.endswith(f"\t{GOPHER_SESSION}")]
    return outer.stdout.strip(), nested


def popup_origin() -> str | None:
    result = tmux("show", "-gv", POPUP_PANE_OPTION)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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


def jump_to(pane: str) -> None:
    outer, nested = popup_clients()
    if outer and nested:
        tmux("switch-client", "-c", outer, "-t", pane)
        for client in nested:
            tmux("detach-client", "-t", client)
    else:
        tmux("switch-client", "-t", pane)


def gopher_agent_pane() -> str | None:
    listing = tmux("list-panes", "-s", "-t", f"={GOPHER_SESSION}:", "-F", "#{pane_id}\t#{pane_current_command}")
    if listing.returncode != 0:
        return None
    panes = [line.split("\t") for line in listing.stdout.splitlines() if "\t" in line]
    return next((pane for pane, command in panes if command == "claude"), None)


def next_level(level: str) -> str:
    return PROACTIVITY_LEVELS[(PROACTIVITY_LEVELS.index(level) + 1) % len(PROACTIVITY_LEVELS)]


def in_gopher_window(own_pane: str | None) -> bool:
    if not own_pane:
        return False
    listing = tmux("list-panes", "-s", "-t", f"={GOPHER_SESSION}:", "-F", "#{pane_id}")
    return listing.returncode == 0 and own_pane in listing.stdout.split()


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


class ProjectCard(ListItem):
    def __init__(self, project: dict, cached: str | None):
        super().__init__(classes=f"card {state_class(project)}")
        self.project_name = project["name"]
        self.project = project
        self.cached = cached

    def parts(self) -> dict[str, Text]:
        project = self.project
        return {
            "name": title_text(project),
            "pstate": project_state_text(project),
            "facts": facts_text(project),
            "agents": agents_text(project["sessions"]),
            "body": body_text(project, self.cached),
        }

    def compose(self) -> ComposeResult:
        parts = self.parts()
        with Horizontal(classes="row"):
            yield Static(parts["name"], classes="name")
            yield Static(parts["pstate"], classes="pstate")
        with Horizontal(classes="row"):
            yield Static(parts["facts"], classes="facts")
            yield Static(parts["agents"], classes="agents")
        yield Static(parts["body"], classes="body")

    def update_card(self, project: dict, cached: str | None) -> None:
        self.project, self.cached = project, cached
        self.remove_class(*STATE_CLASSES).add_class(state_class(project))
        for name, text in self.parts().items():
            self.query_one(f".{name}", Static).update(text)


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
    ListView { height: 1fr; }
    ListItem.card { height: 5; margin-bottom: 1; padding: 0 1; border-left: thick ansi_bright_black; }
    ListItem.card.pstate-active { border-left: thick ansi_green; }
    ListItem.card.pstate-blocked { border-left: thick ansi_red; }
    ListItem.card.pstate-waiting-review { border-left: thick ansi_yellow; }
    ListItem.card .row { height: 1; }
    ListItem.card .name { width: 1fr; }
    ListItem.card .pstate { width: auto; }
    ListItem.card .facts { width: 1fr; padding-left: 2; }
    ListItem.card .agents { width: auto; padding-left: 1; }
    ListItem.card .body { height: 3; padding-left: 2; }
    DetailScreen VerticalScroll { padding: 0 1; }
    MessageScreen { align: center middle; }
    MessageScreen > Static { width: 80%; }
    MessageScreen > Input { width: 80%; }
    """
    BINDINGS = [
        Binding("j", "move(1)", "Down", show=False),
        Binding("k", "move(-1)", "Up", show=False),
        Binding("g", "edge(0)", "Top", show=False),
        Binding("G", "edge(1)", "Bottom", show=False),
        Binding("l", "expand", "Expand"),
        Binding("i", "address", "To Gopher"),
        Binding("t", "jump", "Jump"),
        Binding("x", "mark_seen", "Seen"),
        Binding("m", "message(False)", "Message"),
        Binding("M", "message(True)", "Wake msg", show=False),
        Binding("r", "summarize", "Haiku line"),
        Binding("R", "deep_dive", "Deep dive"),
        Binding("P", "proactivity", "Proactivity", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config):
        super().__init__(ansi_color=True)
        self.theme = "ansi-dark"
        self.config = config
        self.cache = SummaryCache(config)
        self.details = SummaryCache(config, "details")
        self.acks = Acks(config)
        self.focus = FocusTracker()
        self.projects: dict[str, dict] = {}
        self.own_pane = os.environ.get("TMUX_PANE")
        self.in_gopher = in_gopher_window(self.own_pane)
        self.origin: str | None = None

    def compose(self) -> ComposeResult:
        yield ListView()
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        self.set_interval(POPUP_POLL_SECONDS, self.check_origin)

    def check_origin(self) -> None:
        self.run_worker(self.read_origin, thread=True, exclusive=True, group="origin")

    def read_origin(self) -> None:
        origin = popup_origin()
        if origin == self.origin:
            return
        self.origin = origin
        if origin and (name := pane_project(self.config, origin, self.projects)):
            self.call_from_thread(self.highlight, name)

    def highlight(self, name: str) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()
        names = [card.project_name for card in self.cards()]
        if name in names:
            self.query_one(ListView).index = names.index(name)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("address", "deep_dive"):
            return self.in_gopher
        return True

    def cards(self) -> list[ProjectCard]:
        return list(self.query_one(ListView).query(ProjectCard))

    def selected(self) -> dict | None:
        card = self.query_one(ListView).highlighted_child
        return self.projects.get(card.project_name) if isinstance(card, ProjectCard) else None

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
        view = self.query_one(ListView)
        current = self.selected()
        self.projects = {p["name"]: p for p in projects}
        cards = self.cards()
        if [card.project_name for card in cards] == [p["name"] for p in projects]:
            for card, project in zip(cards, projects):
                card.update_card(project, self.cache.get(project))
            return
        await view.clear()
        await view.extend(ProjectCard(project, self.cache.get(project)) for project in projects)
        names = [p["name"] for p in projects]
        view.index = names.index(current["name"]) if current and current["name"] in names else (0 if names else None)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_expand()

    def action_move(self, step: int) -> None:
        view = self.query_one(ListView)
        if step > 0:
            view.action_cursor_down()
        else:
            view.action_cursor_up()

    def action_edge(self, end: int) -> None:
        view = self.query_one(ListView)
        if len(view):
            view.index = len(view) - 1 if end else 0

    def action_expand(self) -> None:
        if project := self.selected():
            self.push_screen(DetailScreen(self, project))

    def send_to_gopher(self, text: str, submit: bool) -> bool:
        pane = gopher_pane(self.own_pane)
        if not pane:
            self.notify("Gopher pane not found in this window", severity="warning")
            return False
        tmux("send-keys", "-t", pane, "-l", text)
        if submit:
            tmux("send-keys", "-t", pane, "Enter")
        tmux("select-pane", "-t", pane)
        return True

    def action_address(self) -> None:
        if project := self.selected():
            self.send_to_gopher(f"[project: {project['name']}] ", submit=False)

    def action_deep_dive(self) -> None:
        if project := self.selected():
            self.send_to_gopher(
                f"[project: {project['name']}] Deep dive: ask its idle agent to refresh STATUS.md and give me a summary.",
                submit=True,
            )

    def action_jump(self) -> None:
        project = self.selected()
        if not project:
            return
        pane = jump_target(project["sessions"])
        if not pane:
            self.notify(f"{project['name']} has no agent in tmux", severity="warning")
            return
        jump_to(pane)

    def action_proactivity(self) -> None:
        level = next_level(gopher_proactivity(self.config))
        set_gopher_setting(self.config.root, "proactivity", level)
        self.config = load_config(self.config.root)
        write_prompt(self.config)
        pane = gopher_agent_pane()
        if pane:
            tmux("send-keys", "-t", pane, "-l", f"Proactivity is now {level}; follow the {level} rules from `gopher prompt gopher`.")
            tmux("send-keys", "-t", pane, "Enter")
            self.notify(f"Proactivity: {level} (told Gopher)")
        else:
            self.notify(f"Proactivity: {level} (Gopher not running; applies at next launch)")

    def action_mark_seen(self) -> None:
        if project := self.selected():
            done = [s["session_id"] for s in project["sessions"] if s.get("attention") == DONE]
            if done:
                self.acks.ack(done)
                self.action_refresh()

    def action_message(self, wake: bool) -> None:
        project = self.selected()
        if not project:
            return

        def file(text: str | None) -> None:
            if text and text.strip():
                path = add_item(Path(project["path"]), text, sender="user", wake=wake)
                self.notify(f"Filed {path.name}")
                self.action_refresh()

        self.push_screen(MessageScreen(project["name"], wake), file)

    def action_summarize(self) -> None:
        if project := self.selected():
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


def run(config: Config) -> None:
    GopherTUI(config).run()
