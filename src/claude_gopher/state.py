import json
import os
import re
import subprocess
import time
from pathlib import Path

from claude_gopher.checkouts import project_for_cwd

IDLE, WORKING, WAITING = "idle", "working", "waiting"
CREDS_GLYPH = "\u2715"

WAITING_NOTIFICATIONS = {"permission_prompt", "elicitation_dialog", "agent_needs_input"}
WAITING_TOOLS = {"AskUserQuestion"}

HOOK_EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "|".join(sorted(WAITING_TOOLS)),
    "PostToolUse": None,
    "PermissionRequest": None,
    "Notification": None,
    "Stop": None,
    "SessionEnd": None,
}


def state_for(payload: dict) -> str | None:
    event = payload.get("hook_event_name")
    if event in ("SessionStart", "Stop"):
        return IDLE
    if event in ("UserPromptSubmit", "PostToolUse"):
        return WORKING
    if event == "PreToolUse":
        return WAITING if payload.get("tool_name") in WAITING_TOOLS else WORKING
    if event == "PermissionRequest":
        return WAITING
    if event == "Notification":
        kind = payload.get("notification_type")
        if kind in WAITING_NOTIFICATIONS:
            return WAITING
        if kind == "idle_prompt":
            return IDLE
    return None


def resolve_project(cwd: str, projects_dir: Path, env: dict) -> str | None:
    if (project := env.get("GOPHER_PROJECT")) and (projects_dir / project).is_dir():
        return project
    if cwd and projects_dir in Path(cwd).parents:
        name = Path(cwd).relative_to(projects_dir).parts[0]
        if (projects_dir / name).is_dir() and not name.startswith("."):
            return name
    return project_for_cwd(projects_dir, cwd)


def tmux_window(pane: str) -> str | None:
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{window_id}"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def session_file(sessions_dir: Path, session_id: str) -> Path:
    return sessions_dir / f"{Path(session_id).name}.json"


def read_record(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
    tmp.replace(path)


def apply_event(payload: dict, sessions_dir: Path, projects_dir: Path, env: dict, now: float | None = None) -> dict | None:
    session_id = payload.get("session_id")
    if not session_id:
        return None
    path = session_file(sessions_dir, session_id)
    if payload.get("hook_event_name") == "SessionEnd":
        path.unlink(missing_ok=True)
        return None
    state = state_for(payload)
    if state is None:
        return None
    record = read_record(path) or {"session_id": session_id}
    cwd = payload.get("cwd") or record.get("cwd") or ""
    cached = record.get("project")
    if "project" not in record or record.get("cwd") != cwd or (cached and not (projects_dir / cached).is_dir()):
        record["project"] = resolve_project(cwd, projects_dir, env)
    pane = env.get("TMUX_PANE")
    if pane and (record.get("pane") != pane or not record.get("tmux_window")):
        record["pane"] = pane
        record["tmux_window"] = tmux_window(pane)
    event = payload.get("hook_event_name")
    record.update(
        state=state,
        event=event,
        cwd=cwd,
        transcript_path=payload.get("transcript_path") or record.get("transcript_path"),
        updated=now if now is not None else time.time(),
    )
    if event == "Stop":
        record["finished"] = record["updated"]
    elif state != IDLE:
        record.pop("finished", None)
    if event == "UserPromptSubmit":
        record.pop("blocked_creds", None)
    write_record(path, record)
    return record


def mark_needs_login(sessions_dir: Path, pane: str, blocked: bool, now: float | None = None) -> list[str]:
    marked = []
    for path in sorted(sessions_dir.glob("*.json")):
        record = read_record(path)
        if not record or record.get("pane") != pane:
            continue
        if blocked:
            record["blocked_creds"] = now if now is not None else time.time()
        else:
            record.pop("blocked_creds", None)
        write_record(path, record)
        marked.append(record.get("session_id") or path.stem)
    return marked


LEADING_GLYPHS = re.compile(r"^[^\w(\[]+\s*")


def pane_locations() -> dict[str, dict] | None:
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{window_name}"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    locations = {}
    for line in out.stdout.splitlines():
        pane, session, window_index, pane_index, window_name = (line.split("\t", 4) + ["", "", "", ""])[:5]
        locations[pane] = {
            "session": session,
            "window_index": window_index,
            "pane_index": pane_index,
            "window_name": LEADING_GLYPHS.sub("", window_name),
            "blocked_creds": window_name.startswith(CREDS_GLYPH),
        }
    return locations


def location_label(location: dict | None, with_name: bool = True) -> str:
    if not location:
        return "-"
    label = f"{location['window_index']}.{location['pane_index']}"
    if location.get("session"):
        label = f"{location['session']}:{label}"
    return f"{label} {location['window_name']}" if with_name and location["window_name"] else label


def list_sessions(sessions_dir: Path, panes: set[str] | None, prune: bool = True) -> list[dict]:
    records = []
    for path in sorted(sessions_dir.glob("*.json")):
        record = read_record(path)
        if record is None:
            continue
        record["_path"] = path
        records.append(record)
    newest_per_pane = {}
    for record in records:
        pane = record.get("pane")
        if pane and record.get("updated", 0) >= newest_per_pane.get(pane, {}).get("updated", -1):
            newest_per_pane[pane] = record
    kept = []
    for record in records:
        pane = record.get("pane")
        stale = pane is not None and (
            (panes is not None and pane not in panes) or newest_per_pane.get(pane) is not record
        )
        path = record.pop("_path")
        if stale:
            if prune:
                path.unlink(missing_ok=True)
            continue
        kept.append(record)
    return sorted(kept, key=lambda r: r.get("updated", 0), reverse=True)


def hook_settings(command: str = "gopher hook state") -> dict:
    hooks = {}
    for event, matcher in HOOK_EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            entry["matcher"] = matcher
        hooks[event] = [entry]
    return {"hooks": hooks}


def merge_sessions(hook_records: list[dict], registry: list[dict] | None, projects_dir: Path) -> list[dict]:
    if registry is None:
        return hook_records
    by_id = {r["session_id"]: r for r in hook_records}
    merged = []
    for live in registry:
        record = dict(live)
        hooked = by_id.get(live["session_id"])
        if hooked:
            record["project"] = hooked.get("project")
            record["transcript_path"] = hooked.get("transcript_path")
            record["finished"] = hooked.get("finished")
            record["blocked_creds"] = hooked.get("blocked_creds")
            if hooked.get("updated", 0) > live.get("updated", 0):
                record.update(state=hooked["state"], event=hooked.get("event"), updated=hooked["updated"])
        else:
            record["project"] = resolve_project(record.get("cwd") or "", projects_dir, {})
        merged.append(record)
    return sorted(merged, key=lambda r: r.get("updated", 0), reverse=True)
