import datetime
import io
import subprocess
from pathlib import Path

import pytest

from claude_gopher import cli
from claude_gopher.checkouts import add_checkout, project_for_cwd, read_checkouts
from claude_gopher.config import ConfigError, Registry, load_config, load_registry, resolve_root, save_registry
from claude_gopher.projects import read_frontmatter
from claude_gopher.roots import RootError, create_project, create_root, ensure_tmp_ignored


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("GOPHER_HOME", raising=False)
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)


def test_config_paths_are_relative_to_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.toml").write_text('[paths]\nstate_dir = "/abs/state"\nagent_dir = "agent"\n')
    config = load_config(root)
    assert config.projects_dir == root / "projects"
    assert config.agent_dir == root / "agent"
    assert config.sessions_dir == Path("/abs/state/sessions")


def test_resolve_root_precedence(tmp_path):
    registry = Registry(default="work", roots={"work": tmp_path / "w", "home": tmp_path / "h"})
    assert resolve_root(env={}, registry=registry) == tmp_path / "w"
    assert resolve_root(env={}, cwd=str(tmp_path / "h" / "projects" / "x"), registry=registry) == tmp_path / "h"
    assert resolve_root(env={"GOPHER_HOME": "home"}, cwd=str(tmp_path / "w"), registry=registry) == tmp_path / "h"
    assert resolve_root(env={"GOPHER_HOME": str(tmp_path / "p")}, registry=registry) == tmp_path / "p"
    assert resolve_root("work", env={"GOPHER_HOME": "home"}, registry=registry) == tmp_path / "w"
    assert resolve_root(env={}, registry=Registry()) == Path("~/workon").expanduser()
    with pytest.raises(ConfigError):
        resolve_root("nope", env={}, registry=registry)


def test_registry_round_trip(tmp_path):
    path = tmp_path / "reg.toml"
    save_registry(Registry(default="a", roots={"a": tmp_path / "a", "b c": tmp_path / "b"}), path)
    registry = load_registry(path)
    assert registry.default == "a"
    assert registry.roots == {"a": tmp_path / "a", "b c": tmp_path / "b"}


def test_create_root_skeleton_and_commit(tmp_path):
    root = tmp_path / "root"
    create_root(root)
    for name in ("config.toml", ".gitignore", "prompts/README.md", "projects", "archived", "closed", "gopher", "reference", "tmp/gopher"):
        assert (root / name).exists(), name
    assert not (root / "gitignore").exists()
    log = subprocess.run(["git", "-C", str(root), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "Create gopher root" in log
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert status == ""


def test_create_root_refuses_non_empty(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "keep").write_text("x")
    with pytest.raises(RootError):
        create_root(tmp_path / "root")
    assert (tmp_path / "root" / "keep").read_text() == "x"


def test_create_root_from_clone(tmp_path):
    create_root(tmp_path / "origin")
    create_root(tmp_path / "copy", source_url=str(tmp_path / "origin"))
    assert (tmp_path / "copy" / "config.toml").is_file()
    assert (tmp_path / "copy" / "tmp" / "gopher").is_dir()


def test_create_project(tmp_path):
    project = create_project(tmp_path, "alpha", "Doing a thing", today=datetime.date(2026, 11, 12))
    fields, _ = read_frontmatter((project / "STATUS.md").read_text())
    assert fields == {"summary": "Doing a thing", "state": "active", "updated": "2026-11-12"}
    with pytest.raises(RootError):
        create_project(tmp_path, "alpha")
    with pytest.raises(RootError):
        create_project(tmp_path, "../escape")


def test_longest_checkout_prefix_wins(tmp_path):
    projects = tmp_path / "projects"
    for name in ("outer", "inner"):
        (projects / name).mkdir(parents=True)
    add_checkout(projects, "outer", tmp_path / "src")
    add_checkout(projects, "inner", tmp_path / "src" / "sub")
    assert project_for_cwd(projects, str(tmp_path / "src" / "sub" / "x")) == "inner"
    assert project_for_cwd(projects, str(tmp_path / "src" / "other")) == "outer"
    assert project_for_cwd(projects, str(tmp_path / "srcfoo")) is None


def test_checkout_paths_under_home_use_tilde(tmp_path):
    (tmp_path / "p").mkdir()
    add_checkout(tmp_path, "p", Path.home() / "src" / "repo")
    assert '"~/src/repo"' in (tmp_path / "p" / "checkouts.toml").read_text()
    assert read_checkouts(tmp_path / "p") == [Path.home() / "src" / "repo"]


def test_cli_new_project_and_move(tmp_path, capsys):
    root = tmp_path / "work"
    assert cli.main(["new", str(root), "--name", "work"]) == 0
    assert load_registry().default == "work"
    assert cli.main(["new", str(tmp_path / "home"), "--name", "home", "--no-default"]) == 0
    assert load_registry().default == "work"
    assert cli.main(["new", str(tmp_path / "again"), "--name", "work"]) == 1

    checkout = tmp_path / "checkout"
    assert cli.main(["project", "new", "a", "--checkout", str(checkout)]) == 0
    assert cli.main(["project", "new", "b"]) == 0
    projects = root / "projects"
    assert read_checkouts(projects / "a") == [checkout]

    assert cli.main(["checkout", "move", str(checkout), "missing"]) == 1
    assert read_checkouts(projects / "a") == [checkout]
    assert cli.main(["checkout", "move", str(checkout), "b"]) == 0
    assert read_checkouts(projects / "a") == []
    assert read_checkouts(projects / "b") == [checkout]
    assert project_for_cwd(projects, str(checkout)) == "b"

    assert cli.main(["--root", "home", "project", "new", "c"]) == 0
    assert (tmp_path / "home" / "projects" / "c" / "STATUS.md").is_file()


def test_hook_ignores_missing_root(tmp_path, monkeypatch):
    root = tmp_path / "absent"
    monkeypatch.setenv("GOPHER_HOME", str(root))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "Stop", "session_id": "s", "cwd": "/"}'))
    assert cli.main(["hook", "state"]) == 0
    assert not root.exists()


def test_tmp_ignored_only_at_root(tmp_path):
    root = tmp_path / "root"
    create_root(root)
    (root / "tmp" / "x").write_text("x")
    (root / "projects" / "p" / "tmp").mkdir(parents=True)
    (root / "projects" / "p" / "tmp" / "keep.md").write_text("x")
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-uall"], capture_output=True, text=True).stdout
    assert status.split() == ["??", "projects/p/tmp/keep.md"]


def test_clone_without_gitignore_gets_one(tmp_path):
    create_root(tmp_path / "origin")
    subprocess.run(["git", "-C", str(tmp_path / "origin"), "rm", "-q", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(tmp_path / "origin"), "commit", "-qm", "drop"], check=True)
    create_root(tmp_path / "copy", source_url=str(tmp_path / "origin"))
    assert (tmp_path / "copy" / ".gitignore").read_text() == "/tmp/\n"


@pytest.mark.parametrize("existing", ["tmp/\n", "tmp\n", "/tmp\n", "*\n!config.toml\n"])
def test_existing_tmp_ignore_is_respected(tmp_path, existing):
    root = tmp_path / "root"
    create_root(root)
    (root / ".gitignore").write_text(existing)
    assert ensure_tmp_ignored(root) is False
    assert (root / ".gitignore").read_text() == existing
