import json

import pytest

from claude_gopher import rename
from claude_gopher.config import load_config
from claude_gopher.roots import RootError, create_project, create_root


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


def test_rename_moves_project_and_state(config):
    (config.state_dir / "inbox" / "alpha").mkdir(parents=True)
    config.sessions_dir.mkdir(parents=True)
    (config.sessions_dir / "s1.json").write_text(json.dumps({"session_id": "s1", "project": "alpha"}))
    (config.sessions_dir / "s2.json").write_text(json.dumps({"session_id": "s2", "project": "other"}))
    report = rename.rename_project(config, "alpha", "beta", windows=False)
    assert not (config.projects_dir / "alpha").exists()
    assert "# beta" in (config.projects_dir / "beta" / "STATUS.md").read_text().splitlines()
    assert (config.state_dir / "inbox" / "beta").is_dir()
    assert report.sessions == ["s1"]
    assert json.loads((config.sessions_dir / "s1.json").read_text())["project"] == "beta"
    assert json.loads((config.sessions_dir / "s2.json").read_text())["project"] == "other"


@pytest.mark.parametrize("old, new, match", [
    ("missing", "beta", "unknown project"),
    ("alpha", "bad name", "invalid project name"),
])
def test_rename_rejects(config, old, new, match):
    with pytest.raises(RootError, match=match):
        rename.rename_project(config, old, new, windows=False)


def test_rename_rejects_existing_target(config):
    create_project(config.projects_dir, "beta")
    with pytest.raises(RootError, match="already exists"):
        rename.rename_project(config, "alpha", "beta", windows=False)
    (config.archived_dir / "gamma").mkdir(parents=True)
    with pytest.raises(RootError, match="already exists"):
        rename.rename_project(config, "alpha", "gamma", windows=False)


def test_rename_warns_about_default_checkouts(config):
    path = config.root / "config.toml"
    path.write_text(path.read_text() + '\n[workon]\ndefault_checkouts = { alpha = "~/src" }\n')
    report = rename.rename_project(load_config(config.root), "alpha", "beta", windows=False)
    assert "default_checkouts" in report.warnings[0]


@pytest.mark.parametrize("name, matched", [
    ("alpha", True), ("\u25cf alpha", True), ("\u2605 alpha", True), ("alpha-2", False), ("my alpha", False),
])
def test_window_pattern(name, matched):
    assert bool(rename.window_pattern("alpha").match(name)) == matched
