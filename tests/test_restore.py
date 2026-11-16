import json

import pytest

from claude_gopher import restore
from claude_gopher.checkouts import add_checkout
from claude_gopher.config import load_config
from claude_gopher.roots import create_project, create_root

LAYOUT = '{"V":2,"L":{"t":"h","w":100,"h":40}}'
SAVE = "\n".join([
    "pane\t0\t1\t0\t:*\t1\thost\t:/root/projects/alpha\t0\tpython3\t:/py /bin/gopher --root /root tui --project alpha",
    "pane\t0\t1\t0\t:*\t2\ttitle\t:/co/alpha\t1\tclaude\t:claude --permission-mode auto",
    "pane\t0\t1\t0\t:*\t3\thost\t:/co/alpha\t0\tzsh\t:",
    "pane\t0\t2\t1\t:-\t1\thost\t:/root/projects/beta\t1\tzsh\t:",
    f"window\t0\t1\t:\u25cb alpha\t0\t:*\t{LAYOUT}\toff",
    "window\t0\t2\t:beta\t1\t:-\t{}\toff",
])


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "root"
    create_root(root)
    config = load_config(root)
    create_project(config.projects_dir, "alpha")
    return config


def write_save(directory):
    save = directory / "tmux_resurrect_1.txt"
    save.write_text(SAVE + "\n")
    (directory / restore.ASSISTANT_FILE).write_text(json.dumps({"sessions": [
        {"tool": "claude", "session_id": "abc-123", "session_name": "0", "window_index": "1", "pane_index": "2"},
        {"tool": "codex", "session_id": "nope", "session_name": "0", "window_index": "1", "pane_index": "3"},
    ]}))
    return save


def test_parse_save_extracts_one_window(tmp_path):
    save = write_save(tmp_path)
    snapshot = restore.parse_save(save, "alpha", restore.assistant_sessions(tmp_path))
    assert snapshot.session == "0" and snapshot.window_index == 1 and snapshot.layout == LAYOUT
    assert [(p.index, p.role, p.claude_session) for p in snapshot.panes] == [
        (1, "sidebar", None), (2, "agent", "abc-123"), (3, "shell", None),
    ]
    assert snapshot.panes[1].active and snapshot.panes[2].directory == "/co/alpha"
    assert restore.parse_save(save, "gamma", {}) is None


def test_snapshot_round_trips(config, tmp_path):
    snapshot = restore.parse_save(write_save(tmp_path), "alpha", restore.assistant_sessions(tmp_path))
    restore.write_snapshot(config, snapshot)
    assert restore.read_snapshot(config, "alpha") == snapshot
    assert restore.read_snapshot(config, "beta") is None


def test_agent_command_resumes_in_owned_checkout(config, tmp_path):
    checkout = tmp_path / "co"
    checkout.mkdir()
    add_checkout(config.projects_dir, "alpha", checkout)
    pane = restore.PaneSnapshot(2, str(checkout), "claude", "", True, "agent", "abc-123")
    command = restore.agent_command(config, "alpha", pane)
    assert command.endswith(f"workon agent alpha {checkout} -- --resume abc-123")
    elsewhere = restore.PaneSnapshot(2, "/tmp", "claude", "", True, "agent")
    assert restore.agent_command(config, "alpha", elsewhere).endswith("workon agent alpha")
    assert restore.pane_command(config, "alpha", restore.PaneSnapshot(3, "/tmp", "zsh", "", False, "shell")) is None


class FakeTmux:
    def __init__(self, windows=""):
        self.calls = []
        self.windows = windows
        self.panes = iter(f"%{n}" for n in range(10, 20))

    def __call__(self, *argv, check=True):
        self.calls.append(argv)
        if argv[0] == "list-windows":
            return self.windows
        if argv[0] in ("new-window", "split-window"):
            return next(self.panes)
        if argv[:2] == ("display-message", "-p"):
            return "@9"
        return ""


def test_open_window_rebuilds_snapshot(config, tmp_path, monkeypatch):
    snapshot = restore.parse_save(write_save(tmp_path), "alpha", restore.assistant_sessions(tmp_path))
    restore.write_snapshot(config, snapshot)
    fake = FakeTmux(windows="0\n2")
    monkeypatch.setattr(restore, "tmux", fake)
    monkeypatch.setattr(restore, "find_window", lambda project: None)
    window, used = restore.open_window(config, "alpha")
    assert window == "@9" and used.source.endswith("tmux_resurrect_1.txt")
    new_window = next(call for call in fake.calls if call[0] == "new-window")
    assert new_window[new_window.index("-t") + 1] == "=0:1"
    assert sum(call[0] == "split-window" for call in fake.calls) == 2
    assert ("select-layout", "-t", "@9", LAYOUT) in fake.calls
    sent = [call[3] for call in fake.calls if call[0] == "send-keys"]
    assert len(sent) == 2 and "tui --project alpha" in sent[0] and sent[1].endswith("--resume abc-123")
    assert ("select-pane", "-t", "%11") in fake.calls


def test_open_window_refuses_when_open_and_falls_back_to_fresh(config, monkeypatch):
    monkeypatch.setattr(restore, "find_window", lambda project: "@3")
    with pytest.raises(restore.RestoreError, match="already has a window"):
        restore.open_window(config, "alpha")
    fake = FakeTmux()
    monkeypatch.setattr(restore, "tmux", fake)
    monkeypatch.setattr(restore, "find_window", lambda project: None)
    _, used = restore.open_window(config, "alpha", session="work")
    assert used.source == "fresh" and [p.role for p in used.panes] == ["sidebar", "agent"]
    new_window = next(call for call in fake.calls if call[0] == "new-window")
    assert new_window[new_window.index("-t") + 1] == "=work:"


def test_close_window_snapshots_then_kills(config, tmp_path, monkeypatch):
    save = write_save(tmp_path)
    fake = FakeTmux()
    monkeypatch.setattr(restore, "tmux", fake)
    monkeypatch.setattr(restore, "find_window", lambda project: "@4")
    monkeypatch.setattr(restore, "save_now", lambda: save)
    monkeypatch.setattr(restore, "keep_gopher_link", lambda session: None)
    snapshot = restore.close_window(config, "alpha")
    assert restore.read_snapshot(config, "alpha") == snapshot
    assert fake.calls[-1] == ("kill-window", "-t", "@4")
