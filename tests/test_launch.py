import shlex
import subprocess

import pytest

from claude_gopher import launch
from claude_gopher.config import load_config
from claude_gopher.roots import create_root


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
    monkeypatch.setenv("GOPHER_HOME", str(root))
    return load_config(root)


def with_settings(config, text: str):
    path = config.root / "config.toml"
    path.write_text(path.read_text() + "\n" + text)
    return load_config(config.root)


class FakeTmux:
    def __init__(self, sessions: dict[str, list[tuple[str, str, str]]]):
        self.sessions = sessions
        self.calls: list[tuple[str, ...]] = []
        self.schedule = False

    def __call__(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        command, rest = argv[0], argv[1:]
        stdout, code = "", 0
        if command == "has-session":
            code = 0 if rest[1].lstrip("=") in self.sessions else 1
        elif command == "list-windows":
            fmt = rest[3]
            windows = self.sessions[rest[1].lstrip("=")]
            if fmt == "#{window_id}":
                stdout = "\n".join(w for _, w, _ in windows)
            else:
                stdout = "\n".join(f"{i} {w}" for i, w, _ in windows)
        elif command == "display-message" and "-t" in rest:
            target = rest[rest.index("-t") + 1]
            stdout = next(n for ws in self.sessions.values() for _, w, n in ws if w == target)
        elif command == "display-message":
            stdout = "main"
        elif command == "new-session":
            self.sessions["gopher"] = [("1", "@9", "gopher")]
            stdout = "%1"
        elif command == "list-panes" and "#{window_name}\t#{pane_id}" in rest:
            stdout = "schedule\t%5" if self.schedule else ""
        elif command == "new-window":
            self.schedule, stdout = True, "%5"
        elif command == "list-panes":
            stdout = "%0\tpython3\n%1\tclaude"
        elif command == "split-window":
            stdout = "%2"
        elif command == "link-window":
            session, index = rest[-1].lstrip("=").split(":")
            self.sessions[session].append((index, rest[2], "gopher"))
        if check and code:
            raise subprocess.CalledProcessError(code, ["tmux", *argv])
        return subprocess.CompletedProcess(argv, code, stdout, "")


def test_gopher_agent_adds_model_name_and_auto_mode(config):
    prompt = ["--append-system-prompt-file", str(launch.prompt_path(config))]
    assert launch.gopher_agent(config).command == [
        "claude", "--model", "sonnet", "--name", "gopher", "--permission-mode", "auto", *prompt,
    ]
    config = with_settings(config, '[agents.claude]\ncommand = ["claude", "--permission-mode", "plan"]')
    assert launch.gopher_agent(config).command == [
        "claude", "--permission-mode", "plan", "--model", "sonnet", "--name", "gopher", *prompt,
    ]


def test_gopher_model_setting(config):
    assert launch.gopher_agent(with_settings(config, '[gopher]\nmodel = "opus"')).command[1:3] == ["--model", "opus"]


def test_gopher_model_can_be_disabled(config):
    assert "--model" not in launch.gopher_agent(with_settings(config, '[gopher]\nmodel = ""')).command


def test_agent_lines_mark_gopher_session_and_kick_off(config):
    config = with_settings(config, '[workon]\npre_launch = ["auth-try"]')
    lines = launch.agent_lines(config, [])
    assert lines[0] == f"cd {config.agent_dir}"
    assert "GOPHER_AGENT=gopher" in lines[1] and f"GOPHER_PROMPT_FILE={launch.prompt_path(config)}" in lines[1]
    assert lines[2] == "auth-try"
    assert shlex.split(lines[3]) == [
        "claude", "--model", "sonnet", "--name", "gopher", "--permission-mode", "auto",
        "--append-system-prompt-file", str(launch.prompt_path(config)), launch.GOPHER_KICKOFF,
    ]
    assert shlex.split(launch.agent_lines(config, ["--continue"])[-1])[-1] == "--continue"


def test_ensure_agent_dir_keeps_existing_status(config):
    launch.ensure_agent_dir(config)
    assert (config.agent_dir / "journal").is_dir() and (config.agent_dir / "pending").is_dir()
    status = config.agent_dir / "STATUS.md"
    assert status.read_text().startswith("---\nsummary:")
    status.write_text("mine")
    launch.ensure_agent_dir(config)
    assert status.read_text() == "mine"


def test_launch_creates_session_and_links_at_zero(config, monkeypatch):
    fake = FakeTmux({"main": [("1", "@1", "work")]})
    monkeypatch.setattr(launch, "tmux", fake)
    report = launch.launch(config, None)
    assert report == ["started gopher session (@9)", "started the schedule loop", "linked into main:0"]
    assert any("schedule run" in c[-2] for c in fake.calls if c[0] == "send-keys")
    assert ("0", "@9", "gopher") in fake.sessions["main"]
    sent = [c[-2] for c in fake.calls if c[0] == "send-keys"]
    assert not any(s.endswith(" tui") for s in sent) and any("launch --exec-agent" in s for s in sent)


def test_launch_is_idempotent(config, monkeypatch):
    fake = FakeTmux({"main": [("0", "@9", "gopher")], "gopher": [("1", "@9", "gopher")]})
    monkeypatch.setattr(launch, "tmux", fake)
    launch.launch(config, "main")
    assert launch.launch(config, "main") == ["gopher session exists (@9)", "schedule loop running", "already linked at main:0"]
    assert not any(c[0] in ("new-session", "link-window") for c in fake.calls)
    assert sum(c[0] == "new-window" for c in fake.calls) == 1


def test_launch_moves_a_window_off_index_zero(config, monkeypatch):
    fake = FakeTmux({"main": [("0", "@1", "editor"), ("1", "@2", "logs"), ("3", "@3", "notes")], "gopher": [("1", "@9", "gopher")]})
    monkeypatch.setattr(launch, "tmux", fake)
    assert launch.launch(config, "main")[-1] == "moved window 'editor' from main:0 to main:2; linked into main:0"
    assert ("move-window", "-s", "=main:0", "-t", "=main:2") in fake.calls
    assert fake.calls.index(("move-window", "-s", "=main:0", "-t", "=main:2")) < next(i for i, c in enumerate(fake.calls) if c[0] == "link-window")


def test_launch_skips_linking_into_gopher_session(config, monkeypatch):
    fake = FakeTmux({"gopher": [("1", "@9", "gopher")]})
    monkeypatch.setattr(launch, "tmux", fake)
    assert launch.launch(config, "gopher") == ["gopher session exists (@9)", "started the schedule loop"]


def test_write_prompt_assembles_gopher_sections(config):
    config = with_settings(config, '[gopher]\nproactivity = "nudge"\nfeatures = ["changelog"]')
    text = launch.write_prompt(config).read_text()
    assert "# Gopher\n" in text and "## Proactivity: nudge" in text and "## Feature: changelog" in text
    assert "## Feature: engineering notebook" not in text


def test_ensure_agent_dir_makes_notebook_when_enabled(config):
    launch.ensure_agent_dir(with_settings(config, '[gopher]\nfeatures = ["notebook"]'))
    assert (config.agent_dir / "notebook").is_dir()


def test_gopher_prompt_names_reference_library(config):
    text = launch.write_prompt(config).read_text()
    assert "## Reference library" in text and str(config.reference_dir) in text


def test_relink_moves_drifted_window_back_to_zero(config, monkeypatch):
    fake = FakeTmux({"main": [("1", "@9", "gopher"), ("2", "@1", "work")], "gopher": [("1", "@9", "gopher")]})
    monkeypatch.setattr(launch, "tmux", fake)
    assert launch.relink("main") == "moved back to main:0 from 1"
    assert ("move-window", "-s", "=main:1", "-t", "=main:0") in fake.calls
    assert launch.launch(config, "main")[-1] == "moved back to main:0 from 1"
    assert not any(c[0] == "link-window" for c in fake.calls)


def test_relink_leaves_occupied_zero_alone(config, monkeypatch):
    fake = FakeTmux({"main": [("0", "@1", "work"), ("1", "@9", "gopher")], "gopher": [("1", "@9", "gopher")]})
    monkeypatch.setattr(launch, "tmux", fake)
    assert launch.relink("main") is None
    assert not any(c[0] == "move-window" for c in fake.calls)


def test_restart_respawns_the_gopher_pane(config, monkeypatch):
    fake = FakeTmux({"main": [("0", "@9", "gopher")], "gopher": [("1", "@9", "gopher")]})
    monkeypatch.setattr(launch, "tmux", fake)
    fake.schedule = True
    assert launch.restart(config, resume=True) == ["restarted Gopher in %1 (continuing its last conversation)", "restarted the schedule loop"]
    assert ("respawn-pane", "-k", "-t", "%1", "-c", str(config.agent_dir)) in fake.calls
    assert ("respawn-pane", "-k", "-t", "%5", "-c", str(config.root)) in fake.calls
    sent = [c[-2] for c in fake.calls if c[0] == "send-keys"]
    assert sent == [f"gopher --root {config.root} launch --exec-agent --continue", launch.schedule_command(config)]
    loop = shlex.split(sent[1])
    assert loop[:2] == ["sh", "-c"] and loop[2].startswith(f"while :; do gopher --root {config.root} schedule run; ")
    assert not any(c[0] in ("new-session", "link-window") for c in fake.calls)


def test_restart_without_a_session_launches(config, monkeypatch):
    fake = FakeTmux({"main": []})
    monkeypatch.setattr(launch, "tmux", fake)
    assert launch.restart(config, resume=False)[0] == "started gopher session (@9)"


def test_resumed_gopher_is_told_to_reschedule(config):
    line = launch.agent_lines(config, [], resume=True)[-1]
    assert "--continue" in line and "just restarted" in line and "Start of session" not in line
    assert "Start of session" in launch.agent_lines(config, [])[-1]


def test_heal_schedule_restarts_only_a_dead_loop(config, monkeypatch):
    fake = FakeTmux({"main": [], "gopher": [("1", "@9", "gopher")]})
    monkeypatch.setattr(launch, "tmux", fake)
    assert launch.heal_schedule(config) == "started the schedule loop"
    fake.schedule = True
    assert launch.heal_schedule(config) == "restarted the schedule loop"
    launch.schedule.loop_lock_path(config).parent.mkdir(parents=True, exist_ok=True)
    with open(launch.schedule.loop_lock_path(config), "w") as held:
        launch.fcntl.flock(held, launch.fcntl.LOCK_EX)
        assert launch.schedule.loop_running(config)
        assert launch.heal_schedule(config) is None
    assert not launch.schedule.loop_running(config)
    del fake.sessions["gopher"]
    assert launch.heal_schedule(config) is None
