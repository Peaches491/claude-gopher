import datetime
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from claude_gopher.config import Config
from claude_gopher.inbox import unread
from claude_gopher.projects import list_projects, read_frontmatter
from claude_gopher.registry import BACKGROUND, read_registry
from claude_gopher.state import IDLE, WAITING, WORKING, list_sessions, merge_sessions, pane_locations

RESOURCE_KINDS = ("prs", "tickets")
DONE, CREDS = "done", "blocked_creds"
TIERS = {WAITING: 0, CREDS: 0, DONE: 0, WORKING: 1, BACKGROUND: 1, IDLE: 2}
URGENCY = (WAITING, CREDS, DONE, WORKING, BACKGROUND, IDLE)
HEADING_LINK = re.compile(r"\]\((\S+)\)\s*$")
NO_AGENT_TIER = 3


def current_sessions(config: Config, prune: bool = True) -> list[dict]:
    registry = read_registry(config.claude_sessions_dir)
    locations = pane_locations()
    panes = None if registry is not None or locations is None else set(locations)
    hook_records = list_sessions(config.sessions_dir, panes, prune=prune and registry is None)
    sessions = merge_sessions(hook_records, registry, config.projects_dir)
    for session in sessions:
        session["location"] = (locations or {}).get(session.get("pane") or "")
    return sessions


CLOSED_STATES = {"closed", "resolved", "done", "merged", "abandoned"}
RECENT_MERGE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Resource:
    name: str
    url: str | None
    state: str = "open"
    merged: float | None = None
    stack: tuple[str, ...] = ()

    @property
    def closed(self) -> bool:
        return self.state in CLOSED_STATES


def parse_when(value: str) -> float | None:
    try:
        when = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if len(value) <= len("YYYY-MM-DD"):
        when += datetime.timedelta(days=1)
    return when.timestamp()


def read_resource(path: Path) -> Resource:
    try:
        fields, body = read_frontmatter(path.read_text())
    except OSError:
        return Resource(path.stem, None)
    first = next((line for line in body.splitlines() if line.strip()), "")
    match = HEADING_LINK.search(first)
    return Resource(
        name=path.stem,
        url=match.group(1) if match else None,
        state=(fields.get("state") or "open").lower(),
        merged=parse_when(fields["merged"]) if fields.get("merged") else None,
        stack=tuple(fields.get("stack", "").replace(",", " ").replace("#", "").split()),
    )


def resource_links(project_dir: Path) -> dict[str, list[Resource]]:
    links = {}
    for kind in RESOURCE_KINDS:
        directory = project_dir / "resources" / kind
        paths = sorted(directory.glob("*.md"), key=lambda p: p.stem, reverse=True) if directory.is_dir() else []
        links[kind] = [read_resource(path) for path in paths]
    return links


def visible_prs(prs: list[Resource], now: float | None = None) -> list[tuple[Resource, int, bool]]:
    now = time.time() if now is None else now
    recent = lambda pr: pr.state == "merged" and pr.merged is not None and now - pr.merged < RECENT_MERGE_SECONDS
    shown = [pr for pr in prs if not pr.closed or recent(pr)]
    out, stacks_done = [], set()
    for pr in shown:
        if pr.stack and pr.name in pr.stack:
            if pr.stack in stacks_done:
                continue
            members = [p for p in shown if p.stack == pr.stack]
            top = max(members, key=lambda p: pr.stack.index(p.name) if p.name in pr.stack else -1)
            stacks_done.add(pr.stack)
            out.append((top, len(pr.stack), recent(top)))
        else:
            out.append((pr, 1, recent(pr)))
    return out


def open_tickets(tickets: list[Resource]) -> list[Resource]:
    return [ticket for ticket in tickets if not ticket.closed]


class Acks:
    def __init__(self, config: Config):
        self.path = config.state_dir / "tui" / "acks.json"

    def load(self) -> dict[str, float]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def ack(self, session_ids: list[str], when: float | None = None) -> None:
        data = self.load()
        for session_id in session_ids:
            data[session_id] = when if when is not None else time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))


def attention(session: dict, acks: dict[str, float]) -> str | None:
    if (session.get("location") or {}).get("blocked_creds"):
        return CREDS
    finished = session.get("finished")
    if session.get("state") == IDLE and finished and finished > acks.get(session.get("session_id"), 0):
        return DONE
    return None


def effective_state(session: dict) -> str | None:
    return session.get("attention") or session.get("state")


def urgency(state: str | None) -> int:
    return URGENCY.index(state) if state in URGENCY else len(URGENCY)


def most_urgent(sessions: list[dict]) -> dict | None:
    return min(sessions, key=lambda s: urgency(effective_state(s)), default=None)


def agent_tier(project: dict) -> int:
    top = most_urgent(project["sessions"])
    return TIERS.get(effective_state(top), 2) if top else NO_AGENT_TIER


def status_day(project: dict) -> int:
    try:
        return datetime.date.fromisoformat(project.get("updated") or "").toordinal()
    except ValueError:
        return 0


YELLOW, RED, GREEN, GREY = range(4)
AGENT_GROUP = {WAITING: YELLOW, CREDS: RED}
STATUS_GROUP = {"waiting-review": YELLOW, "blocked": RED, "active": GREEN}


def colour_group(project: dict) -> int:
    top = most_urgent(project["sessions"])
    agent = AGENT_GROUP.get(effective_state(top), GREY) if top else GREY
    return min(agent, STATUS_GROUP.get(project.get("state") or "", GREY))


def project_rank(project: dict) -> tuple:
    top = most_urgent(project["sessions"])
    kind = effective_state(top) if top else None
    times = [s.get("updated", 0) for s in project["sessions"] if effective_state(s) == kind]
    if kind in (WAITING, CREDS):
        recency = min(times)
    elif times:
        recency = -max(times)
    else:
        recency = -status_day(project)
    return (colour_group(project), urgency(kind), recency, project["name"])


def project_overview(config: Config, sessions: list[dict] | None = None) -> list[dict]:
    sessions = current_sessions(config) if sessions is None else sessions
    acks = Acks(config).load()
    for session in sessions:
        session["attention"] = attention(session, acks)
    projects = list_projects(config.projects_dir, sessions)
    for project in projects:
        project_dir = Path(project["path"])
        items = unread(project_dir)
        project["inbox"] = {
            "unread": len(items),
            "wake": sum(item.wake for item in items),
            "items": [item.path.name for item in items],
        }
        project["links"] = resource_links(project_dir)
        project["resources"] = {
            "prs": len(visible_prs(project["links"]["prs"])),
            "tickets": len(open_tickets(project["links"]["tickets"])),
        }
    projects.sort(key=project_rank)
    return projects
