import json
import os
from pathlib import Path

from claude_gopher import registry
from claude_gopher.checkouts import add_checkout
from claude_gopher.registry import BACKGROUND, parse_tmux, proc_start, read_registry
from claude_gopher.state import IDLE, WAITING, WORKING, merge_sessions


def entry(pid, session_id="s1", status="idle", **extra):
    return {
        "pid": pid, "sessionId": session_id, "status": status, "cwd": "/nowhere",
        "tmux": "0:@3.%9", "name": "demo-1", "kind": "interactive", "statusUpdatedAt": 2000, **extra,
    }


def write(directory, data, name=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name or data['pid']}.json").write_text(json.dumps(data))


def test_parse_tmux():
    assert parse_tmux("0:@3.%9") == ("@3", "%9")
    assert parse_tmux(None) == (None, None)
    assert parse_tmux("garbage") == (None, None)


def test_live_entry_is_read_and_mapped(tmp_path):
    pid = os.getpid()
    write(tmp_path, entry(pid, status="shell", procStart=proc_start(pid)))
    [session] = read_registry(tmp_path)
    assert session == {
        "session_id": "s1", "name": "demo-1", "kind": "interactive", "state": BACKGROUND,
        "cwd": "/nowhere", "pane": "%9", "tmux_window": "@3", "pid": pid, "updated": 2.0,
    }


def test_status_mapping():
    assert registry.STATUS_TO_STATE == {"busy": WORKING, "idle": IDLE, "waiting": WAITING, "shell": BACKGROUND}


def test_dead_and_reused_pids_are_skipped(tmp_path):
    pid = os.getpid()
    write(tmp_path, entry(2**22 + 12345, session_id="dead"))
    write(tmp_path, entry(pid, session_id="reused", procStart="1"), name="reused")
    (tmp_path / "junk.json").write_text("{not json")
    assert read_registry(tmp_path) == []


def test_missing_registry_dir_returns_none(tmp_path):
    assert read_registry(tmp_path / "absent") is None


def live(session_id, updated, state=IDLE, cwd="/nowhere"):
    return {"session_id": session_id, "state": state, "updated": updated, "cwd": cwd, "pane": "%1"}


def test_merge_prefers_newer_hook_state_and_keeps_project():
    hooks = [{"session_id": "a", "state": WAITING, "event": "PermissionRequest", "updated": 10,
              "project": "p", "transcript_path": "/t"}]
    [merged] = merge_sessions(hooks, [live("a", 5)], Path("/no/projects"))
    assert (merged["state"], merged["project"], merged["transcript_path"]) == (WAITING, "p", "/t")


def test_merge_prefers_newer_registry_state():
    hooks = [{"session_id": "a", "state": WAITING, "updated": 1, "project": "p"}]
    [merged] = merge_sessions(hooks, [live("a", 5, state=IDLE)], Path("/no/projects"))
    assert (merged["state"], merged["project"]) == (IDLE, "p")


def test_merge_drops_hook_records_absent_from_registry():
    assert merge_sessions([{"session_id": "gone", "state": IDLE, "updated": 1}], [], Path("/no/projects")) == []


def test_merge_without_registry_falls_back_to_hooks():
    hooks = [{"session_id": "a", "state": IDLE, "updated": 1}]
    assert merge_sessions(hooks, None, Path("/no/projects")) == hooks


def test_merge_resolves_project_for_registry_only_sessions(tmp_path):
    projects = tmp_path / "projects"
    (projects / "linked").mkdir(parents=True)
    add_checkout(projects, "linked", tmp_path / "repo")
    [merged] = merge_sessions([], [live("a", 1, cwd=str(tmp_path / "repo"))], projects)
    assert merged["project"] == "linked"
