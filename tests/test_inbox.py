import datetime
import io
import json

import pytest

from claude_gopher import cli
from claude_gopher.checkouts import add_checkout
from claude_gopher.config import load_config
from claude_gopher.inbox import INBOX_LIMIT, add_item, inbox_context, read_item, unread
from claude_gopher.roots import create_project, create_root

NOW = datetime.datetime(2026, 11, 12, 14, 3, 7)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("GOPHER_PROJECT", "GOPHER_AGENT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "root"
    create_root(path)
    monkeypatch.setenv("GOPHER_HOME", str(path))
    create_project(path / "projects", "alpha")
    return path


def test_add_item_format_and_collisions(root):
    project = root / "projects" / "alpha"
    first = add_item(project, "Ping Bob about the PR!", sender="gopher", wake=True, now=NOW)
    second = add_item(project, "Ping Bob about the PR!", now=NOW)
    assert first.name == "20261112-140307-ping-bob-about-the-pr.md"
    assert second.name == "20261112-140307-ping-bob-about-the-pr-2.md"
    item = read_item(first)
    assert (item.sender, item.created, item.wake, item.body) == ("gopher", "2026-11-12T14:03", True, "Ping Bob about the PR!")
    (project / "inbox" / "read").mkdir()
    (project / "inbox" / "read" / "old.md").write_text("---\nfrom: x\n---\nold\n")
    assert [i.name for i in unread(project)] == [first.name, second.name]


def test_context_full_then_reminder(root):
    project = root / "projects" / "alpha"
    items = [read_item(add_item(project, "hello", now=NOW))]
    assert "### inbox/" in inbox_context(project, items, set())
    reminder = inbox_context(project, items, {items[0].name})
    assert reminder.startswith("Inbox: 1 earlier item") and "hello" not in reminder
    assert inbox_context(project, [], set()) == ""
    big = [read_item(add_item(project, "x" * INBOX_LIMIT, now=NOW))]
    assert len(inbox_context(project, big, set())) == INBOX_LIMIT


def hook(monkeypatch, capsys, command, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", **payload})))
    assert cli.main(["hook", command]) == 0
    out = capsys.readouterr().out
    return json.loads(out) if out else None


def test_prompt_hook_delivers_once(root, monkeypatch, capsys):
    cwd = str(root / "projects" / "alpha")
    prompt = {"hook_event_name": "UserPromptSubmit", "cwd": cwd}
    assert hook(monkeypatch, capsys, "inbox", prompt) is None
    assert cli.main(["inbox", "add", "alpha", "rebase the stack"]) == 0
    capsys.readouterr()
    first = hook(monkeypatch, capsys, "inbox", prompt)["hookSpecificOutput"]
    assert first["hookEventName"] == "UserPromptSubmit" and "rebase the stack" in first["additionalContext"]
    second = hook(monkeypatch, capsys, "inbox", prompt)["hookSpecificOutput"]["additionalContext"]
    assert "earlier item" in second and "rebase the stack" not in second
    assert "Finish what the user asked first" in first["additionalContext"]
    assert hook(monkeypatch, capsys, "inbox", {**prompt, "cwd": "/elsewhere"}) is None


def test_session_start_includes_inbox_and_resets_on_compact(root, monkeypatch, capsys):
    cwd = str(root / "projects" / "alpha")
    add_item(root / "projects" / "alpha", "check CI", now=NOW)
    start = {"hook_event_name": "SessionStart", "cwd": cwd, "source": "startup"}
    output = hook(monkeypatch, capsys, "context", start)
    text = output["hookSpecificOutput"]["additionalContext"]
    assert "# Project agent: alpha" in text and "check CI" in text
    assert "ask the user what they would like to do" in text and "Do not start on any" in text
    assert output["systemMessage"] == "gopher: project alpha, 1 unread inbox item(s)"
    resumed = hook(monkeypatch, capsys, "context", {**start, "source": "resume"})["hookSpecificOutput"]["additionalContext"]
    assert "check CI" not in resumed and "earlier item" in resumed
    compacted = hook(monkeypatch, capsys, "context", {**start, "source": "compact"})["hookSpecificOutput"]["additionalContext"]
    assert "check CI" in compacted


def test_stop_hook_wakes_once_for_wake_items(root, monkeypatch, capsys):
    cwd = str(root / "projects" / "alpha")
    stop = {"hook_event_name": "Stop", "cwd": cwd}
    add_item(root / "projects" / "alpha", "not urgent", now=NOW)
    assert hook(monkeypatch, capsys, "inbox", stop) is None
    assert cli.main(["inbox", "add", "alpha", "--wake", "--from", "gopher", "urgent"]) == 0
    capsys.readouterr()
    decision = hook(monkeypatch, capsys, "inbox", stop)
    assert decision["decision"] == "block" and "urgent" in decision["reason"] and "not urgent" not in decision["reason"]
    assert "subagent" in decision["reason"]
    assert hook(monkeypatch, capsys, "inbox", stop) is None
    prompt = hook(monkeypatch, capsys, "inbox", {"hook_event_name": "UserPromptSubmit", "cwd": cwd})
    assert "### inbox/" in prompt["hookSpecificOutput"]["additionalContext"]
    assert "urgent\n" not in prompt["hookSpecificOutput"]["additionalContext"].split("not urgent")[-1]


def test_tracking_lives_in_state_dir(root, monkeypatch, capsys):
    add_item(root / "projects" / "alpha", "hi", now=NOW)
    hook(monkeypatch, capsys, "inbox", {"hook_event_name": "UserPromptSubmit", "cwd": str(root / "projects" / "alpha")})
    assert (load_config(root).state_dir / "inbox" / "alpha" / "s1.seen").is_file()
    assert sorted(p.name for p in (root / "projects" / "alpha" / "inbox").iterdir()) == [
        p.name for p in unread(root / "projects" / "alpha")
    ]


def test_inbox_list_and_errors(root, capsys):
    assert cli.main(["inbox", "add", "alpha", "--wake", "line one"]) == 0
    capsys.readouterr()
    assert cli.main(["inbox", "list"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "wake" in out and "line one" in out
    assert cli.main(["inbox", "add", "missing", "x"]) == 1


def test_inbox_add_from_stdin_and_flag_after_text(root, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    assert cli.main(["inbox", "add", "alpha"]) == 0
    assert cli.main(["inbox", "add", "alpha", "text first", "--wake"]) == 0
    items = unread(root / "projects" / "alpha")
    assert [(i.body, i.wake) for i in items] == [("from stdin", False), ("text first", True)]


def test_notice_without_items(root, monkeypatch, capsys):
    output = hook(monkeypatch, capsys, "context", {"hook_event_name": "SessionStart", "cwd": str(root / "projects" / "alpha")})
    assert output["systemMessage"] == "gopher: project alpha"


def test_list_and_projects_overview(root, tmp_path, monkeypatch, capsys):
    create_project(root / "projects", "beta", "Second one")
    add_item(root / "projects" / "beta", "urgent", wake=True, now=NOW)
    add_item(root / "projects" / "beta", "later", now=NOW)
    add_checkout(root / "projects", "beta", tmp_path / "co")
    monkeypatch.setattr(cli, "current_sessions", lambda config: [{"session_id": "x", "project": "beta", "state": "waiting"}])
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.splitlines() == ["alpha", "beta   Second one"]
    assert cli.main(["list", "--names"]) == 0
    assert capsys.readouterr().out.split() == ["alpha", "beta"]
    assert cli.main(["projects"]) == 0
    header, first, second = capsys.readouterr().out.splitlines()
    assert header.split() == ["PROJECT", "STATE", "AGENT", "INBOX", "CO", "SUMMARY"]
    assert first.split()[:5] == ["beta", "active", "waiting", "2!", "1"]
    assert second.split()[:5] == ["alpha", "active", "-", "-", "0"]
