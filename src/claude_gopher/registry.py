import json
import os
from pathlib import Path

from claude_gopher.state import IDLE, WAITING, WORKING

BACKGROUND = "background"
STATUS_TO_STATE = {"busy": WORKING, "idle": IDLE, "waiting": WAITING, "shell": BACKGROUND}


def proc_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


def is_alive(entry: dict) -> bool:
    pid = entry.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    expected = entry.get("procStart")
    return expected is None or proc_start(pid) == str(expected)


def parse_tmux(value: str | None) -> tuple[str | None, str | None]:
    if not value or "." not in value:
        return None, None
    window, pane = value.rsplit(".", 1)
    return window.split(":", 1)[-1] or None, pane or None


def to_session(entry: dict) -> dict | None:
    session_id = entry.get("sessionId")
    if not session_id:
        return None
    window, pane = parse_tmux(entry.get("tmux"))
    updated_ms = entry.get("statusUpdatedAt") or entry.get("updatedAt") or entry.get("startedAt") or 0
    return {
        "session_id": session_id,
        "name": entry.get("name"),
        "kind": entry.get("kind"),
        "state": STATUS_TO_STATE.get(entry.get("status"), entry.get("status")),
        "cwd": entry.get("cwd"),
        "pane": pane,
        "tmux_window": window,
        "pid": entry.get("pid"),
        "updated": updated_ms / 1000,
    }


def read_registry(sessions_dir: Path) -> list[dict] | None:
    if not sessions_dir.is_dir():
        return None
    sessions = []
    for path in sessions_dir.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and is_alive(entry) and (session := to_session(entry)):
            sessions.append(session)
    return sessions
