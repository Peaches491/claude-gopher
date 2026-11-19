import subprocess

import pytest

from claude_gopher import cli, update, workon
from claude_gopher.config import ConfigError, load_config
from claude_gopher.prompt import assemble
from claude_gopher.roots import create_root


def run(*argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    source = tmp_path / "source"
    source.mkdir()
    run("git", "init", "-q", cwd=source)
    run("git", "commit", "-q", "--allow-empty", "-m", "x", cwd=source)
    for tag in ("v0.1.0", "v0.10.0", "v0.9.1", "nightly", "v1.0.0-rc1"):
        run("git", "tag", tag, cwd=source)
    root = tmp_path / "root"
    create_root(root)
    (root / "config.toml").write_text((root / "config.toml").read_text() + f'\n[gopher]\nupdate_source = "{source}"\n')
    monkeypatch.setenv("GOPHER_HOME", str(root))
    monkeypatch.setattr(update, "editable_dir", lambda: None)
    return load_config(root)


def test_check_picks_the_highest_release_tag(config, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda: "0.9.1")
    status = update.check(config)
    assert status.latest == "0.10.0" and status.available
    assert "v0.10.0 is available (installed 0.9.1)" in status.describe()
    assert update.install_command(status)[-1] == f"git+{status.source}@v0.10.0"
    monkeypatch.setattr(update, "installed_version", lambda: "0.10.0")
    assert not update.check(config).available and "up to date" in update.check(config).describe()


def test_update_without_a_terminal_needs_yes(config, monkeypatch, capsys):
    monkeypatch.setattr(update, "installed_version", lambda: "0.1.0")
    installed = []
    monkeypatch.setattr(update, "install", lambda status: installed.append(status.latest))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["update"]) == 2 and installed == []
    assert cli.main(["update", "--check"]) == 0 and installed == []
    assert cli.main(["update", "--yes"]) == 0 and installed == ["0.10.0"]
    assert "gopher launch --restart --continue" in capsys.readouterr().out


def test_editable_installs_are_not_reinstalled(config, monkeypatch, tmp_path):
    monkeypatch.setattr(update, "installed_version", lambda: "0.1.0")
    monkeypatch.setattr(update, "editable_dir", lambda: tmp_path / "checkout")
    status = update.check(config)
    assert "git pull" in status.describe()
    with pytest.raises(update.UpdateError, match="editable"):
        update.install(status)


def test_gopher_prompt_points_at_the_nightly_check(config):
    text = assemble(config, "gopher")
    assert "Update checks are on" in text and "schedule/update-check.md" in text
    path = config.root / "config.toml"
    path.write_text(path.read_text().replace("[gopher]\n", "[gopher]\nupdate_check = false\n"))
    assert "Update checks are off" in assemble(load_config(config.root), "gopher")
    path.write_text(path.read_text().replace("update_check = false", 'update_check = "yes"'))
    with pytest.raises(ConfigError):
        assemble(load_config(config.root), "gopher")


def test_restart_sidebar_respawns_only_the_sidebar(config, monkeypatch):
    calls, sent = [], []

    def tmux(*argv):
        calls.append(argv)
        return "%1\tclaude\n%2\tpython3" if argv[0] == "list-panes" else ""

    monkeypatch.setattr(workon, "tmux", tmux)
    monkeypatch.setattr(workon, "send", lambda pane, text: sent.append((pane, text)))
    assert workon.restart_sidebar(config, "@3", "alpha") == "alpha: sidebar restarted"
    assert ("respawn-pane", "-k", "-t", "%2", "-c", str(config.projects_dir / "alpha")) in calls
    assert sent == [("%2", workon.sidebar_command(config, "alpha"))]


def test_shell_init_tmux_points_at_the_packaged_config(capsys):
    assert cli.main(["shell-init", "tmux", "--path"]) == 0
    path = capsys.readouterr().out.strip()
    assert path.endswith("claude_gopher/shell/tmux.conf") and "prefix g" in open(path).read()


def test_editable_installs_report_the_checkout_version(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "claude-gopher"\nversion = "0.4.2"\n')
    monkeypatch.setattr(update, "editable_dir", lambda: tmp_path)
    assert update.installed_version() == "0.4.2"
