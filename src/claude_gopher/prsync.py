import datetime
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from claude_gopher.checkouts import project_dirs
from claude_gopher.config import Config
from claude_gopher.inbox import add_item
from claude_gopher.overview import read_resource

PULL_URL = re.compile(r"^https?://[^/\s]+/[^/\s]+/[^/\s]+/pull/\d+")
LIVE_STATES = ("open", "draft")
GH_TIMEOUT = 30
WORKERS = 8


class SyncError(Exception):
    pass


@dataclass
class Change:
    project: str
    path: Path
    old: str
    new: str
    merged: str | None = None
    closed: str | None = None

    def describe(self) -> str:
        if self.new == "merged":
            return f"#{self.path.stem} merged" + (f" {self.merged.replace('T', ' ')}" if self.merged else "")
        if self.new == "closed":
            return f"#{self.path.stem} closed without merging" + (f" {self.closed.replace('T', ' ')}" if self.closed else "")
        return f"#{self.path.stem} {self.old} -> {self.new}"


def tracked(config: Config) -> list[tuple[str, Path, str, str]]:
    found = []
    for project in project_dirs(config.projects_dir):
        directory = project / "resources" / "prs"
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            resource = read_resource(path)
            if resource.state in LIVE_STATES and resource.url and PULL_URL.match(resource.url):
                found.append((project.name, path, resource.state, resource.url))
    return found


def local_minute(stamp: str | None) -> str | None:
    if not stamp:
        return None
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%dT%H:%M")


def lookup(url: str) -> tuple[str, str | None]:
    try:
        result = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state,isDraft,mergedAt,closedAt"],
            capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SyncError(f"{url}: gh timed out after {GH_TIMEOUT}s") from None
    if result.returncode != 0:
        raise SyncError(f"{url}: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'gh failed'}")
    data = json.loads(result.stdout)
    state = data.get("state", "").upper()
    if state == "MERGED":
        return "merged", local_minute(data.get("mergedAt"))
    if state == "CLOSED":
        return "closed", local_minute(data.get("closedAt"))
    return ("draft" if data.get("isDraft") else "open"), None


def set_state(path: Path, state: str, when: str | None) -> None:
    text = path.read_text()
    lines = text.splitlines()
    updates = {"state": state, **({state: when} if when and state in ("merged", "closed") else {})}
    if lines and lines[0].strip() == "---" and "---" in (line.strip() for line in lines[1:]):
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
        head = lines[1:end]
        for key, value in updates.items():
            index = next((i for i, line in enumerate(head) if line.split(":", 1)[0].strip() == key and not line.startswith((" ", "\t"))), None)
            if index is None:
                head.append(f"{key}: {value}")
            else:
                head[index] = f"{key}: {value}"
        lines = ["---", *head, *lines[end:]]
    else:
        lines = ["---", *(f"{key}: {value}" for key, value in updates.items()), "---", *lines]
    path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") or not text else ""))


def sync(config: Config, dry_run: bool = False) -> tuple[list[Change], list[str]]:
    items = tracked(config)
    if items and not shutil.which("gh"):
        raise SyncError("the GitHub CLI (gh) is not installed")

    def check(item):
        project, path, old, url = item
        try:
            new, when = lookup(url)
        except SyncError as error:
            return None, f"{project}: {error}"
        change = Change(project, path, old, new, when if new == "merged" else None, when if new == "closed" else None)
        return (change if new != old else None), None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(check, items))
    changes = [change for change, _ in results if change]
    errors = [error for _, error in results if error]
    if not dry_run:
        for change in changes:
            set_state(change.path, change.new, change.merged or change.closed)
        by_project: dict[str, list[Change]] = {}
        for change in changes:
            by_project.setdefault(change.project, []).append(change)
        for project, project_changes in by_project.items():
            summary = "; ".join(change.describe() for change in project_changes)
            add_item(
                config.projects_dir / project,
                f"PR status update: {summary}. The resources/prs/ files are updated to match.",
                sender="gopher",
            )
    return changes, errors
