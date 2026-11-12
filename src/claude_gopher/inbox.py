import datetime
import re
from dataclasses import dataclass
from pathlib import Path

from claude_gopher.projects import read_frontmatter

INBOX_LIMIT = 8_000
RESET_SOURCES = {"compact", "clear"}
SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Item:
    path: Path
    sender: str
    created: str
    wake: bool
    body: str

    @property
    def name(self) -> str:
        return self.path.name


def inbox_dir(project_dir: Path) -> Path:
    return project_dir / "inbox"


def read_item(path: Path) -> Item:
    fields, body = read_frontmatter(path.read_text())
    return Item(
        path=path,
        sender=fields.get("from", "?"),
        created=fields.get("created", ""),
        wake=fields.get("wake", "").lower() == "true",
        body=body.strip(),
    )


def unread(project_dir: Path) -> list[Item]:
    directory = inbox_dir(project_dir)
    if not directory.is_dir():
        return []
    return [read_item(path) for path in sorted(directory.glob("*.md"), key=lambda p: p.stem) if path.is_file()]


def add_item(
    project_dir: Path, text: str, sender: str = "user", wake: bool = False, now: datetime.datetime | None = None
) -> Path:
    if not project_dir.is_dir():
        raise FileNotFoundError(f"no project at {project_dir}")
    now = now or datetime.datetime.now()
    directory = inbox_dir(project_dir)
    directory.mkdir(exist_ok=True)
    slug = SLUG.sub("-", text.lower()).strip("-")[:40].rstrip("-") or "note"
    stem = f"{now:%Y%m%d-%H%M%S}-{slug}"
    path = directory / f"{stem}.md"
    counter = 2
    while path.exists():
        path = directory / f"{stem}-{counter}.md"
        counter += 1
    path.write_text(
        f"---\nfrom: {sender}\ncreated: {now:%Y-%m-%dT%H:%M}\nwake: {'true' if wake else 'false'}\n---\n{text.strip()}\n"
    )
    return path


class Tracker:
    def __init__(self, state_dir: Path, project: str, session_id: str):
        self.directory = state_dir / "inbox" / project
        self.session = Path(session_id).name

    def _file(self, kind: str) -> Path:
        return self.directory / f"{self.session}.{kind}"

    def get(self, kind: str) -> set[str]:
        path = self._file(kind)
        return set(path.read_text().split()) if path.is_file() else set()

    def add(self, kind: str, names: set[str]) -> None:
        if not names:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self._file(kind).write_text("\n".join(sorted(self.get(kind) | names)) + "\n")

    def reset(self, kind: str) -> None:
        self._file(kind).unlink(missing_ok=True)


def format_item(item: Item, project_dir: Path) -> str:
    rel = item.path.relative_to(project_dir)
    header = f"### {rel} (from {item.sender}{', ' + item.created if item.created else ''})"
    return f"{header}\n\n{item.body}"


START, PROMPT = "start", "prompt"
FRAMING = {
    START: (
        "Summarize these in one line each when you first reply, alongside where the project stands, then ask the "
        "user what they would like to do. Do not start on any of them unless the user picks it."
    ),
    PROMPT: (
        "Finish what the user asked first, then add one line at the end of your reply naming the new item(s). "
        "Do not switch to them unless the user asks."
    ),
}
SESSION_KICKOFF = "Start of session: read your status, summarize your inbox, and ask me what I'd like to do next."


def inbox_context(project_dir: Path, items: list[Item], seen: set[str], moment: str = PROMPT) -> str:
    if not items:
        return ""
    new = [item for item in items if item.name not in seen]
    old = len(items) - len(new)
    parts = []
    if new:
        parts.append(f"## Inbox: {len(new)} new item(s) for this project\n\n{FRAMING[moment]}")
        parts.extend(format_item(item, project_dir) for item in new)
    if old:
        parts.append(f"Inbox: {old} earlier item(s) still unread in `{inbox_dir(project_dir)}`; mention only if asked.")
    text = "\n\n".join(parts)
    if len(text) > INBOX_LIMIT:
        note = f"\n\n[truncated; read the files in {inbox_dir(project_dir)}]"
        text = text[: INBOX_LIMIT - len(note)] + note
    return text


def inbox_notice(project: str, items: list[Item], seen: set[str]) -> str:
    new = sum(item.name not in seen for item in items)
    wake = sum(item.wake for item in items)
    parts = [f"gopher: project {project}"]
    if items:
        parts.append(f"{len(items)} unread inbox item(s)" + (f", {new} new" if new and new != len(items) else ""))
    if wake:
        parts.append(f"{wake} marked wake")
    return ", ".join(parts)


def wake_reason(project_dir: Path, items: list[Item], woken: set[str]) -> tuple[str, set[str]]:
    pending = [item for item in items if item.wake and item.name not in woken]
    if not pending:
        return "", set()
    body = "\n\n".join(format_item(item, project_dir) for item in pending)
    reason = (
        "Inbox item(s) marked wake: the user wants these acted on now. Unless you are waiting on the user's answer to "
        "a question (then mention the item and keep waiting), hand each one to a subagent with the item text and "
        f"`{project_dir}`: it does the work, records anything durable in the project (notes/, STATUS.md, "
        "resources/), and moves the item to inbox/read/. Then tell the user in a line or two what was done."
        f"\n\n{body}"
    )
    return reason[:INBOX_LIMIT], {item.name for item in pending}
