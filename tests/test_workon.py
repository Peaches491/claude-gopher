import shlex
import subprocess
from pathlib import Path

import pytest

from claude_gopher import cli, workon
from claude_gopher.checkouts import add_checkout, read_checkouts
from claude_gopher.config import load_config
from claude_gopher.inbox import SESSION_KICKOFF
from claude_gopher.roots import create_project, create_root


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("GOPHER_PROJECT", "GOPHER_AGENT", workon.EVAL_ENV):
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


def git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def write_config(root: Path, extra: str) -> None:
    (root / "config.toml").write_text((root / "config.toml").read_text() + "\n" + extra)


class FakeUI:
    def __init__(self, menu=(), confirm=(), read=()):
        self.menus, self.confirms, self.reads = list(menu), list(confirm), list(read)
        self.prompts = []

    def say(self, text):
        pass

    def menu(self, prompt, options):
        self.prompts.append((prompt, options))
        return self.menus.pop(0)

    def confirm(self, prompt):
        return self.confirms.pop(0)

    def read(self, prompt):
        return self.reads.pop(0)


def test_agents_from_config(root):
    write_config(root, '[workon]\ndefault_agent = "cursor"\n\n[agents.cursor]\ncommand = ["cursor-agent", "--auto-review"]\nresume_flags = ["--resume"]\n\n[agents.claude]\ncommand = ["claude", "--permission-mode", "auto"]\n')
    config = load_config(root)
    assert workon.pick_agent(config, None).command == ["cursor-agent", "--auto-review"]
    claude = workon.pick_agent(config, "claude")
    assert claude.command == ["claude", "--permission-mode", "auto"] and "-c" in claude.resume_flags
    with pytest.raises(workon.WorkonError):
        workon.pick_agent(config, "nope")


def test_agent_argv_kickoff_unless_resuming():
    agent = workon.DEFAULT_AGENTS["claude"]
    assert workon.agent_argv(agent, ["--model", "x"]) == ["claude", "--model", "x", SESSION_KICKOFF]
    assert workon.agent_argv(agent, ["--resume=abc"]) == ["claude", "--resume=abc"]
    assert workon.agent_argv(agent, ["-c"]) == ["claude", "-c"]


def test_setup_commands_match_and_render(root, tmp_path):
    write_config(root, f'[[checkouts.setup]]\nmatch = "{tmp_path}/mono/*"\ncommands = ["env-tool activate {{{{name}}}}", "echo {{{{project}}}}"]\n\n[workon]\npre_launch = ["auth-try"]\n')
    config = load_config(root)
    assert workon.setup_commands(config, "alpha", tmp_path / "mono" / "feat") == ["env-tool activate feat", "echo alpha"]
    assert workon.setup_commands(config, "alpha", tmp_path / "other") == []
    lines = workon.source_lines(config, "alpha", tmp_path / "mono" / "feat")
    assert lines[0] == f"cd {tmp_path / 'mono' / 'feat'}"
    assert f"GOPHER_HOME={root}" in lines[1] and "GOPHER_PROJECT=alpha" in lines[1]
    assert lines[2:] == [
        "env-tool activate feat", "echo alpha", "auth-try",
        'if [ -n "${TMUX_PANE:-}" ]; then tmux rename-window -t "$TMUX_PANE" alpha; fi',
    ]


def test_resolve_checkout_rules(root, tmp_path):
    config = load_config(root)
    projects = config.projects_dir
    one = git_repo(tmp_path / "one")
    two = git_repo(tmp_path / "two")
    stranger = git_repo(tmp_path / "stranger")
    assert workon.resolve_checkout(config, FakeUI(), "alpha", None, str(tmp_path)) is None
    assert workon.resolve_checkout(config, FakeUI(confirm=[True]), "alpha", None, str(stranger)) == stranger
    assert read_checkouts(projects / "alpha") == [stranger]
    add_checkout(projects, "alpha", one)
    add_checkout(projects, "alpha", two)
    assert workon.resolve_checkout(config, FakeUI(), "alpha", None, str(two)) == two
    assert workon.resolve_checkout(config, FakeUI(), "alpha", "one", str(tmp_path)) == one
    ui = FakeUI(menu=[f"two  ({two})"])
    assert workon.resolve_checkout(config, ui, "alpha", None, str(tmp_path)) == two
    assert len(ui.prompts[0][1]) == 4
    assert ui.prompts[0][1][0] == f"project directory  ({projects / 'alpha'})"
    assert workon.resolve_checkout(config, FakeUI(menu=[ui.prompts[0][1][0]]), "alpha", None, str(tmp_path)) is None
    with pytest.raises(workon.WorkonError):
        workon.resolve_checkout(config, FakeUI(), "alpha", "missing", str(tmp_path))


def test_source_from_a_checkout_offers_the_others(root, tmp_path):
    config = load_config(root)
    one, two = git_repo(tmp_path / "one"), git_repo(tmp_path / "two")
    add_checkout(config.projects_dir, "alpha", one)
    add_checkout(config.projects_dir, "alpha", two)
    write_config(root, f'[workon]\ndefault_checkouts = {{ alpha = "{one}" }}\n')
    config = load_config(root)
    assert workon.resolve_checkout(config, FakeUI(), "alpha", None, str(two)) == one
    ui = FakeUI(menu=[f"one  ({one})"])
    assert workon.resolve_checkout(config, ui, "alpha", None, str(two), switch=True) == one
    assert ui.prompts[0][1][2] == f"two  ({two})  <- here"
    ui = FakeUI(menu=[f"two  ({two})"])
    assert workon.resolve_checkout(config, ui, "alpha", None, str(config.projects_dir / "alpha"), switch=True) == two
    assert ui.prompts[0][1][0].endswith("<- here")
    assert workon.resolve_checkout(config, FakeUI(), "alpha", None, str(tmp_path), switch=True) == one


def test_default_checkout_wins(root, tmp_path):
    write_config(root, f'[workon]\ndefault_checkouts = {{ alpha = "{tmp_path}/ref" }}\n')
    assert workon.resolve_checkout(load_config(root), FakeUI(), "alpha", None, str(tmp_path)) == tmp_path / "ref"


def test_resolve_project_menu_and_new(root, tmp_path, monkeypatch):
    monkeypatch.delenv("GOPHER_PROJECT", raising=False)
    config = load_config(root)
    create_project(config.projects_dir, "beta")
    here = git_repo(tmp_path / "co")
    add_checkout(config.projects_dir, "beta", here)
    assert workon.resolve_project(config, FakeUI(), None, str(here)) == "beta"
    ui = FakeUI(menu=["alpha"])
    assert workon.resolve_project(config, ui, None, str(tmp_path)) == "alpha"
    assert ui.prompts[0][1] == ["alpha", "beta", workon.NEW_PROJECT]
    assert workon.resolve_project(config, FakeUI(menu=[workon.NEW_PROJECT], read=["gamma"]), None, str(tmp_path)) == "gamma"
    assert (config.projects_dir / "gamma" / "STATUS.md").is_file()
    with pytest.raises(workon.WorkonError):
        workon.resolve_project(config, FakeUI(), "nope", str(tmp_path))


def test_cli_agent_eval_mode_prints_script(root, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(workon.EVAL_ENV, "1")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["workon", "agent", "alpha", "--", "--model", "opus"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"cd {root / 'projects' / 'alpha'}"
    assert shlex.split(lines[-1]) == ["claude", "--model", "opus", SESSION_KICKOFF]


def test_cli_source_needs_shell_function(root, capsys):
    assert cli.main(["workon", "source", "alpha"]) == 1
    assert "shell-init" in capsys.readouterr().err


def test_cli_link_unlink_where(root, tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    assert cli.main(["workon", "link", "alpha"]) == 0
    assert read_checkouts(root / "projects" / "alpha") == [repo]
    assert cli.main(["workon", "where", "alpha"]) == 0
    assert str(repo) in capsys.readouterr().out
    assert cli.main(["workon", "unlink", "alpha"]) == 0
    assert read_checkouts(root / "projects" / "alpha") == []
    assert cli.main(["workon", "unlink", "alpha"]) == 1
    monkeypatch.chdir(root)
    assert cli.main(["workon", "link", "alpha"]) == 1


def test_dash_dash_rejected_elsewhere(root):
    with pytest.raises(SystemExit):
        cli.main(["projects", "--", "x"])


def test_completions(root, tmp_path, capsys):
    config = load_config(root)
    add_checkout(config.projects_dir, "alpha", tmp_path / "co")
    assert ("agent", workon.ACTIONS["agent"]) in workon.completions(config, [])
    assert [v for v, _ in workon.completions(config, ["agent"])] == ["alpha"]
    assert workon.completions(config, ["agent", "alpha"]) == [("co", str(tmp_path / "co"))]
    assert [v for v, _ in workon.completions(config, ["term", "--agent"])] == ["claude"]
    assert [v for v, _ in workon.completions(config, ["term", "--agent", "claude"])] == ["alpha"]
    assert workon.completions(config, ["agent", "alpha", "co"]) == []
    assert cli.main(["workon", "complete", "--zsh", "--", "agent"]) == 0
    assert capsys.readouterr().out.startswith("alpha")


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_shell_init_parses(shell, capsys):
    assert cli.main(["shell-init", shell]) == 0
    script = capsys.readouterr().out
    if subprocess.run(["which", shell], capture_output=True).returncode == 0:
        assert subprocess.run([shell, "-n", "-c", script]).returncode == 0


def test_resolve_project_detects_project_dir_and_env(root, tmp_path, monkeypatch):
    monkeypatch.delenv("GOPHER_PROJECT", raising=False)
    config = load_config(root)
    create_project(config.projects_dir, "beta")
    inside = config.projects_dir / "beta" / "plan"
    inside.mkdir(exist_ok=True)
    assert workon.resolve_project(config, FakeUI(), None, str(inside)) == "beta"
    assert workon.resolve_project(config, FakeUI(), None, str(config.projects_dir / "beta")) == "beta"
    monkeypatch.setenv("GOPHER_PROJECT", "alpha")
    assert workon.resolve_project(config, FakeUI(), None, str(tmp_path)) == "alpha"
    monkeypatch.setenv("GOPHER_PROJECT", "gone")
    assert workon.resolve_project(config, FakeUI(menu=["beta"]), None, str(tmp_path)) == "beta"
    shared = git_repo(tmp_path / "shared")
    add_checkout(config.projects_dir, "alpha", shared)
    add_checkout(config.projects_dir, "beta", shared)
    monkeypatch.setenv("GOPHER_PROJECT", "beta")
    assert workon.resolve_project(config, FakeUI(), None, str(shared)) == "beta"
