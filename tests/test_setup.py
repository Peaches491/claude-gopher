import argparse
import json

import pytest

from claude_gopher.config import load_registry
from claude_gopher.hooks import plan_hooks, unplan_hooks
from claude_gopher.roots import RootError, create_root
from claude_gopher.roots import create_project
from claude_gopher.setup import NEW, REVIEW, SetupIO, install_dir, kickoff_message, run_setup
from claude_gopher.state import HOOK_EVENTS


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


class FakeIO:
    def __init__(self, answers=(), confirms=()):
        self.answers = list(answers)
        self.confirms = list(confirms)
        self.said = []
        self.launched = []

    def io(self):
        return SetupIO(
            ask=lambda prompt, default: self.answers.pop(0) if self.answers else default,
            confirm=lambda prompt, default: self.confirms.pop(0) if self.confirms else default,
            say=self.said.append,
            launch=lambda root, start: self.launched.append((root, start)),
        )


def setup_args(**overrides):
    values = dict(dir=None, name=None, source=None, default=None, no_hooks=False, no_interview=False, review=False, yes=False, remove_hooks=False)
    return argparse.Namespace(**{**values, **overrides})


def test_plan_hooks_is_idempotent_and_keeps_other_hooks():
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other.sh"}]}]}, "model": "x"}
    updated, added = plan_hooks(settings, "/bin/gopher")
    assert len(added) == len(HOOK_EVENTS) + 3
    assert updated["model"] == "x"
    assert updated["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "other.sh"
    assert {"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": "/bin/gopher hook state"}]} in updated["hooks"]["PreToolUse"]
    assert settings["hooks"]["SessionStart"] == [{"hooks": [{"type": "command", "command": "other.sh"}]}]
    again, added_again = plan_hooks(updated, "/other/gopher")
    assert added_again == [] and again == updated


def test_setup_new_root_installs_hooks_and_launches_interview(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    fake = FakeIO()
    run_setup(setup_args(dir=str(tmp_path / "work")), fake.io(), settings=settings)
    assert (tmp_path / "work" / "config.toml").is_file()
    assert load_registry().default == "work"
    written = json.loads(settings.read_text())
    assert written["theme"] == "dark" and "SessionStart" in written["hooks"]
    assert list(tmp_path.glob("settings.json.bak-gopher-*"))
    assert fake.launched == [(tmp_path / "work", NEW)]


def test_setup_existing_root_repairs_and_offers_review(tmp_path):
    root = tmp_path / "cloned"
    create_root(root)
    (root / ".gitignore").write_text("")
    (root / "tmp" / "gopher").rmdir()
    fake = FakeIO(answers=["shared"], confirms=[True])
    run_setup(setup_args(dir=str(root), no_hooks=True), fake.io())
    assert (root / "tmp" / "gopher").is_dir()
    assert (root / ".gitignore").read_text() == "/tmp/\n"
    assert load_registry().roots == {"shared": root}
    assert fake.launched == [(root, REVIEW)]


def test_setup_declines(tmp_path):
    settings = tmp_path / "settings.json"
    fake = FakeIO(confirms=[False, False])
    run_setup(setup_args(dir=str(tmp_path / "work")), fake.io(), settings=settings)
    assert not settings.exists()
    assert fake.launched == []


def test_setup_review_requires_root(tmp_path):
    with pytest.raises(RootError):
        run_setup(setup_args(dir=str(tmp_path / "nothing"), review=True), FakeIO().io())
    create_root(tmp_path / "r")
    fake = FakeIO()
    run_setup(setup_args(dir=str(tmp_path / "r"), review=True), fake.io())
    assert fake.launched == [(tmp_path / "r", REVIEW)]


def test_setup_refuses_non_root_non_empty(tmp_path):
    (tmp_path / "junk").mkdir()
    (tmp_path / "junk" / "f").write_text("x")
    with pytest.raises(RootError):
        run_setup(setup_args(dir=str(tmp_path / "junk"), no_hooks=True, no_interview=True), FakeIO().io())
    assert load_registry().roots == {}


def test_kickoff_message_describes_root(tmp_path):
    root = tmp_path / "r"
    create_root(root)
    message = kickoff_message(root, NEW)
    assert f"`{install_dir()}/prompts/setup.md`" in message
    assert "none yet" in message and "Projects in this root: 0." in message
    assert "new root" in message
    assert "`Configure gopher root via setup interview`" in message
    (root / "prompts" / "all.md").write_text("x")
    create_project(root / "projects", "alpha")
    message = kickoff_message(root, REVIEW)
    assert "files: all.md." in message and "(alpha)" in message and "This is a review" in message
    assert (install_dir() / "prompts" / "setup.md").is_file()


def test_remove_hooks_undoes_install_and_keeps_other_hooks(tmp_path):
    original = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other.sh"}]}]}, "model": "x"}
    installed, _ = plan_hooks(original, "/bin/gopher")
    restored, removed = unplan_hooks(installed)
    assert restored == original and len(removed) == len(HOOK_EVENTS) + 3
    assert unplan_hooks(restored) == (original, [])
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(installed))
    fake = FakeIO()
    assert run_setup(setup_args(remove_hooks=True, yes=True), fake.io(), settings=settings) == 0
    assert json.loads(settings.read_text()) == original and "removed gopher hooks" in fake.said[-1]
