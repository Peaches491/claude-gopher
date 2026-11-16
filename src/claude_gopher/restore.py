import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from claude_gopher.checkouts import read_checkouts
from claude_gopher import launch
from claude_gopher.config import Config

SAVE_SCRIPT_OPTION = "@resurrect-save-script-path"
DIR_OPTION = "@resurrect-dir"
ASSISTANT_FILE = "assistant-sessions.json"
SAVE_TIMEOUT = 60
GLYPH_PREFIX = re.compile(r"^[^\w(\[]+\s*")


class RestoreError(Exception):
    pass


@dataclass
class PaneSnapshot:
    index: int
    directory: str
    command: str
    full_command: str
    active: bool
    role: str
    claude_session: str | None = None


@dataclass
class WindowSnapshot:
    project: str
    session: str
    layout: str
    saved_at: float
    source: str
    panes: list[PaneSnapshot] = field(default_factory=list)
    window_index: int | None = None


def tmux(*argv: str, check: bool = True) -> str:
    result = subprocess.run(["tmux", *argv], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RestoreError(result.stderr.strip() or f"tmux {argv[0]} failed")
    return result.stdout.strip()


def option(name: str) -> str:
    return tmux("show", "-gqv", name, check=False)


def resurrect_dir() -> Path:
    configured = option(DIR_OPTION)
    if configured:
        return Path(configured).expanduser()
    legacy = Path("~/.tmux/resurrect").expanduser()
    if legacy.is_dir():
        return legacy
    data = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
    return data / "tmux" / "resurrect"


def save_now() -> Path:
    script = option(SAVE_SCRIPT_OPTION)
    if not script or not Path(script).is_file():
        raise RestoreError("tmux-resurrect is not loaded (no @resurrect-save-script-path)")
    result = subprocess.run([script, "quiet"], capture_output=True, text=True, timeout=SAVE_TIMEOUT, check=False)
    last = resurrect_dir() / "last"
    if result.returncode != 0 or not last.exists():
        raise RestoreError(f"tmux-resurrect save failed: {(result.stderr or result.stdout).strip() or last}")
    return last.resolve()


def strip_colon(value: str) -> str:
    return value[1:] if value.startswith(":") else value


def window_name(name: str) -> str:
    return GLYPH_PREFIX.sub("", name)


def assistant_sessions(directory: Path) -> dict[tuple[str, str, str], dict]:
    path = directory / ASSISTANT_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        (entry.get("session_name", ""), str(entry.get("window_index", "")), str(entry.get("pane_index", ""))): entry
        for entry in data.get("sessions", [])
        if entry.get("tool") == "claude" and entry.get("session_id")
    }


def pane_role(command: str, full_command: str, claude_session: str | None) -> str:
    if "gopher" in full_command and " tui" in full_command:
        return "sidebar"
    if command == "claude" or claude_session:
        return "agent"
    return "shell"


def parse_save(save: Path, project: str, assistants: dict[tuple[str, str, str], dict]) -> WindowSnapshot | None:
    windows, panes = {}, {}
    for line in save.read_text().splitlines():
        fields = line.split("\t")
        if fields[0] == "window" and len(fields) >= 7:
            session, index, name, layout = fields[1], fields[2], strip_colon(fields[3]), fields[6]
            windows[(session, index)] = (window_name(name), layout)
        elif fields[0] == "pane" and len(fields) >= 11:
            panes.setdefault((fields[1], fields[2]), []).append(fields)
    for (session, index), (name, layout) in windows.items():
        if name != project:
            continue
        snapshot = WindowSnapshot(project, session, layout, time.time(), str(save), window_index=int(index))
        for fields in sorted(panes.get((session, index), []), key=lambda f: int(f[5])):
            claude = assistants.get((session, index, fields[5]), {}).get("session_id")
            full_command = strip_colon(fields[10])
            snapshot.panes.append(PaneSnapshot(
                index=int(fields[5]), directory=strip_colon(fields[7]), command=fields[9],
                full_command=full_command, active=fields[8] == "1",
                role=pane_role(fields[9], full_command, claude), claude_session=claude,
            ))
        return snapshot
    return None


def snapshot_path(config: Config, project: str) -> Path:
    return config.state_dir / "windows" / f"{project}.json"


def write_snapshot(config: Config, snapshot: WindowSnapshot) -> Path:
    path = snapshot_path(config, snapshot.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(snapshot), indent=2) + "\n")
    return path


def read_snapshot(config: Config, project: str) -> WindowSnapshot | None:
    path = snapshot_path(config, project)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    data["panes"] = [PaneSnapshot(**pane) for pane in data.get("panes", [])]
    return WindowSnapshot(**data)


def find_window(project: str) -> str | None:
    for line in tmux("list-windows", "-a", "-F", "#{window_id}\t#{window_name}", check=False).splitlines():
        window, _, name = line.partition("\t")
        if window_name(name) == project:
            return window
    return None


def capture(config: Config, project: str) -> WindowSnapshot:
    save = save_now()
    snapshot = parse_save(save, project, assistant_sessions(save.parent))
    if not snapshot:
        raise RestoreError(f"no window named {project} in the tmux-resurrect save")
    write_snapshot(config, snapshot)
    return snapshot


def close_window(config: Config, project: str) -> WindowSnapshot:
    window = find_window(project)
    if not window:
        raise RestoreError(f"{project} has no open window")
    snapshot = capture(config, project)
    tmux("kill-window", "-t", window)
    keep_gopher_link(snapshot.session)
    return snapshot


def keep_gopher_link(session: str) -> None:
    launch.relink(session)


def agent_command(config: Config, project: str, pane: PaneSnapshot) -> str:
    argv = ["gopher", "--root", str(config.root), "workon", "agent", project]
    if Path(pane.directory) in read_checkouts(config.projects_dir / project):
        argv.append(pane.directory)
    if pane.claude_session:
        argv += ["--", "--resume", pane.claude_session]
    return shlex.join(argv)


def pane_command(config: Config, project: str, pane: PaneSnapshot) -> str | None:
    if pane.role == "sidebar":
        return shlex.join(["gopher", "--root", str(config.root), "tui", "--project", project])
    if pane.role == "agent":
        return agent_command(config, project, pane)
    return None


def fresh_snapshot(config: Config, project: str, session: str) -> WindowSnapshot:
    directory = str(config.projects_dir / project)
    checkouts = read_checkouts(config.projects_dir / project)
    agent_dir = str(checkouts[0]) if checkouts else directory
    return WindowSnapshot(project, session, "", time.time(), "fresh", [
        PaneSnapshot(1, directory, "", "gopher tui", False, "sidebar"),
        PaneSnapshot(2, agent_dir, "claude", "", True, "agent"),
    ])


def open_window(config: Config, project: str, session: str | None = None) -> tuple[str, WindowSnapshot]:
    if find_window(project):
        raise RestoreError(f"{project} already has a window")
    snapshot = read_snapshot(config, project)
    target = session or (snapshot.session if snapshot else None) or tmux("display-message", "-p", "#{session_name}")
    if not snapshot or not snapshot.panes:
        snapshot = fresh_snapshot(config, project, target)
    panes = snapshot.panes
    taken = tmux("list-windows", "-t", f"={target}:", "-F", "#{window_index}", check=False).splitlines()
    index = snapshot.window_index if snapshot.window_index is not None and str(snapshot.window_index) not in taken else ""
    first = tmux("new-window", "-d", "-t", f"={target}:{index}", "-n", project, "-c", panes[0].directory, "-P", "-F", "#{pane_id}")
    window = tmux("display-message", "-p", "-t", first, "#{window_id}")
    ids = [first]
    for pane in panes[1:]:
        ids.append(tmux("split-window", "-d", "-t", ids[-1], "-c", pane.directory, "-P", "-F", "#{pane_id}"))
    if snapshot.layout:
        tmux("select-layout", "-t", window, snapshot.layout, check=False)
    else:
        tmux("resize-pane", "-t", ids[0], "-x", "20%", check=False)
        tmux("select-layout", "-t", window, "main-vertical", check=False)
        tmux("resize-pane", "-t", ids[0], "-x", "20%", check=False)
    for pane_id, pane in zip(ids, panes):
        if command := pane_command(config, project, pane):
            tmux("send-keys", "-t", pane_id, command, "Enter")
    active = next((pane_id for pane_id, pane in zip(ids, panes) if pane.active), ids[-1])
    tmux("select-pane", "-t", active)
    return window, snapshot
