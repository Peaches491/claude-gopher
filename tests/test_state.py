import json

import pytest

from claude_gopher import state
from claude_gopher.checkouts import add_checkout
from claude_gopher.state import IDLE, WAITING, WORKING, apply_event, list_sessions, state_for


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    monkeypatch.setattr(state, "tmux_window", lambda pane: f"@win-{pane}")


def event(name, **extra):
    return {"hook_event_name": name, "session_id": "s1", "cwd": "/nowhere", **extra}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (event("SessionStart", source="startup"), IDLE),
        (event("UserPromptSubmit", prompt="hi"), WORKING),
        (event("PreToolUse", tool_name="Bash"), WORKING),
        (event("PreToolUse", tool_name="AskUserQuestion"), WAITING),
        (event("PostToolUse", tool_name="AskUserQuestion"), WORKING),
        (event("PermissionRequest", tool_name="Bash"), WAITING),
        (event("Notification", notification_type="permission_prompt"), WAITING),
        (event("Notification", notification_type="elicitation_dialog"), WAITING),
        (event("Notification", notification_type="idle_prompt"), IDLE),
        (event("Notification", notification_type="auth_success"), None),
        (event("Stop"), IDLE),
        (event("SubagentStop"), None),
    ],
)
def test_state_for(payload, expected):
    assert state_for(payload) == expected


def test_lifecycle_writes_and_removes_record(tmp_path):
    sessions = tmp_path / "sessions"
    env = {"TMUX_PANE": "%7", "GOPHER_PROJECT": "demo"}
    (tmp_path / "projects" / "demo").mkdir(parents=True)
    apply_event(event("SessionStart", transcript_path="/t.jsonl"), sessions, tmp_path / "projects", env, now=1.0)
    apply_event(event("UserPromptSubmit"), sessions, tmp_path / "projects", env, now=2.0)
    record = json.loads((sessions / "s1.json").read_text())
    assert record == {
        "session_id": "s1",
        "state": WORKING,
        "event": "UserPromptSubmit",
        "project": "demo",
        "pane": "%7",
        "tmux_window": "@win-%7",
        "cwd": "/nowhere",
        "transcript_path": "/t.jsonl",
        "updated": 2.0,
    }
    apply_event(event("SessionEnd", reason="other"), sessions, tmp_path / "projects", env)
    assert not (sessions / "s1.json").exists()


def test_ignored_event_leaves_record_untouched(tmp_path):
    sessions = tmp_path / "sessions"
    apply_event(event("Stop"), sessions, tmp_path / "projects", {}, now=1.0)
    assert apply_event(event("SubagentStop"), sessions, tmp_path / "projects", {}, now=5.0) is None
    assert json.loads((sessions / "s1.json").read_text())["updated"] == 1.0


def test_missing_session_id_is_ignored(tmp_path):
    assert apply_event({"hook_event_name": "Stop"}, tmp_path, tmp_path / "projects", {}) is None
    assert not list(tmp_path.iterdir())


def test_session_id_cannot_escape_dir(tmp_path):
    sessions = tmp_path / "sessions"
    apply_event(event("Stop", session_id="../../evil"), sessions, tmp_path / "projects", {})
    assert [p.name for p in sessions.iterdir()] == ["evil.json"]


def own(projects_dir, project, *paths):
    (projects_dir / project).mkdir(parents=True, exist_ok=True)
    for path in paths:
        add_checkout(projects_dir, project, path)


def test_project_from_checkouts(tmp_path):
    projects = tmp_path / "projects"
    repo = tmp_path / "repo"
    own(projects, "alpha", repo)
    record = apply_event(event("Stop", cwd=str(repo / "sub")), tmp_path / "s", projects, {})
    assert record["project"] == "alpha"


def test_project_recomputed_when_cwd_changes(tmp_path):
    projects = tmp_path / "projects"
    repo = tmp_path / "repo"
    own(projects, "alpha", repo)
    sessions = tmp_path / "s"
    assert apply_event(event("Stop", cwd=str(tmp_path)), sessions, projects, {})["project"] is None
    assert apply_event(event("Stop", cwd=str(repo)), sessions, projects, {})["project"] == "alpha"


def test_env_project_wins(tmp_path):
    (tmp_path / "projects" / "env").mkdir(parents=True)
    record = apply_event(event("Stop"), tmp_path, tmp_path / "projects", {"GOPHER_PROJECT": "env"})
    assert record["project"] == "env"


def test_env_project_for_missing_project_falls_back_to_checkout(tmp_path):
    projects = tmp_path / "projects"
    repo = tmp_path / "repo"
    own(projects, "renamed", repo)
    record = apply_event(event("Stop", cwd=str(repo)), tmp_path / "s", projects, {"GOPHER_PROJECT": "old-name"})
    assert record["project"] == "renamed"


def write(sessions, sid, pane, updated):
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{sid}.json").write_text(json.dumps({"session_id": sid, "pane": pane, "updated": updated}))


def test_list_prunes_dead_panes_and_duplicates(tmp_path):
    write(tmp_path, "dead", "%1", 5)
    write(tmp_path, "old", "%2", 1)
    write(tmp_path, "new", "%2", 2)
    write(tmp_path, "nopane", None, 3)
    sessions = list_sessions(tmp_path, panes={"%2"})
    assert [s["session_id"] for s in sessions] == ["nopane", "new"]
    assert sorted(p.stem for p in tmp_path.glob("*.json")) == ["new", "nopane"]


def test_list_without_tmux_keeps_everything_but_duplicates(tmp_path):
    write(tmp_path, "a", "%1", 1)
    write(tmp_path, "b", "%1", 2)
    assert [s["session_id"] for s in list_sessions(tmp_path, panes=None, prune=False)] == ["b"]
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_hook_settings_shape():
    hooks = state.hook_settings()["hooks"]
    assert set(hooks) == set(state.HOOK_EVENTS)
    assert hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion"
    assert "matcher" not in hooks["Stop"][0]


def test_cached_project_re_resolved_after_rename(tmp_path):
    projects = tmp_path / "projects"
    repo = tmp_path / "repo"
    own(projects, "old", repo)
    sessions = tmp_path / "s"
    assert apply_event(event("Stop", cwd=str(repo)), sessions, projects, {})["project"] == "old"
    (projects / "old").rename(projects / "new")
    assert apply_event(event("Stop", cwd=str(repo)), sessions, projects, {})["project"] == "new"


def test_finished_set_on_stop_and_cleared_on_next_turn(tmp_path):
    sessions = tmp_path / "sessions"
    env = {"TMUX_PANE": "%7"}
    apply_event(event("UserPromptSubmit"), sessions, tmp_path / "projects", env, now=1.0)
    apply_event(event("Stop"), sessions, tmp_path / "projects", env, now=2.0)
    apply_event(event("Notification", notification_type="idle_prompt"), sessions, tmp_path / "projects", env, now=3.0)
    assert json.loads((sessions / "s1.json").read_text())["finished"] == 2.0
    apply_event(event("UserPromptSubmit"), sessions, tmp_path / "projects", env, now=4.0)
    assert "finished" not in json.loads((sessions / "s1.json").read_text())
