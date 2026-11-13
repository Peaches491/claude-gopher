import json
import re
import subprocess
from dataclasses import dataclass, field

from claude_gopher.config import Config
from claude_gopher.roots import PROJECT_NAME, RootError


@dataclass
class RenameReport:
    windows: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check(config: Config, old: str, new: str) -> None:
    if not (config.projects_dir / old).is_dir():
        raise RootError(f"unknown project {old!r} in {config.projects_dir}")
    if not PROJECT_NAME.match(new):
        raise RootError(f"invalid project name {new!r}")
    for directory in (config.projects_dir, config.archived_dir, config.closed_dir):
        if (directory / new).exists():
            raise RootError(f"{directory / new} already exists")


def rename_heading(config: Config, old: str, new: str) -> None:
    status = config.projects_dir / new / "STATUS.md"
    if status.is_file():
        text = status.read_text()
        status.write_text(re.sub(rf"^# {re.escape(old)}$", f"# {new}", text, count=1, flags=re.M))


def rename_state(config: Config, old: str, new: str) -> list[str]:
    tracker = config.state_dir / "inbox" / old
    if tracker.is_dir() and not (config.state_dir / "inbox" / new).exists():
        tracker.rename(config.state_dir / "inbox" / new)
    moved = []
    if not config.sessions_dir.is_dir():
        return moved
    for path in sorted(config.sessions_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("project") == old:
            record["project"] = new
            path.write_text(json.dumps(record, indent=2, sort_keys=True))
            moved.append(record.get("session_id", path.stem))
    return moved


def window_pattern(name: str) -> re.Pattern:
    return re.compile(rf"^(\W+\s)?{re.escape(name)}$")


def rename_windows(old: str, new: str) -> list[str]:
    try:
        listing = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", "#{window_id}\t#{session_name}:#{window_index}\t#{window_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if listing.returncode != 0:
        return []
    pattern = window_pattern(old)
    renamed = []
    for line in listing.stdout.splitlines():
        window, where, name = line.split("\t", 2)
        if match := pattern.match(name):
            subprocess.run(["tmux", "rename-window", "-t", window, f"{match.group(1) or ''}{new}"], capture_output=True)
            renamed.append(where)
    return renamed


def rename_project(config: Config, old: str, new: str, windows: bool = True) -> RenameReport:
    check(config, old, new)
    (config.projects_dir / old).rename(config.projects_dir / new)
    rename_heading(config, old, new)
    report = RenameReport(sessions=rename_state(config, old, new))
    if windows:
        report.windows = rename_windows(old, new)
    if old in config.settings.get("workon", {}).get("default_checkouts", {}):
        report.warnings.append(f"config.toml [workon] default_checkouts still names {old!r}; rename that key by hand")
    return report
