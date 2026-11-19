import json

import pytest

from claude_gopher import doctor, schedule
from claude_gopher.cli import main
from claude_gopher.checkouts import add_checkout
from claude_gopher.config import load_config
from claude_gopher.hooks import plan_hooks
from claude_gopher.roots import create_project, create_root


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
    return load_config(root)


def levels(checks, area):
    return [check.level for check in checks if check.area == area]


def test_hooks_missing_and_stale(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    assert levels(doctor.check_hooks(path), "hooks") == [doctor.FAIL]
    settings, _ = plan_hooks({}, "/nowhere/bin/gopher")
    path.write_text(json.dumps(settings))
    checks = doctor.check_hooks(path)
    assert [c.level for c in checks] == [doctor.FAIL] and "missing gopher" in checks[0].message
    settings, _ = plan_hooks({}, "gopher")
    path.write_text(json.dumps(settings))
    monkey = doctor.shutil.which
    try:
        doctor.shutil.which = lambda name: "/usr/bin/gopher"
        assert levels(doctor.check_hooks(path), "hooks") == [doctor.OK]
    finally:
        doctor.shutil.which = monkey


def test_backup_without_upstream_fails(config):
    assert levels(doctor.check_backup(config, 0), "backup") == [doctor.FAIL]


def test_schedule_problems_and_heartbeat(config):
    (config.schedule_dir / "broken.md").write_text("---\nevery: soon\n---\n")
    checks = doctor.check_schedule(config, 1e9)
    assert doctor.FAIL in levels(checks, "schedule") and levels(checks, "heartbeat") == [doctor.WARN]


def test_checkouts_missing_and_shared(config, tmp_path):
    for name in ("a", "b"):
        create_project(config.projects_dir, name)
    shared = tmp_path / "shared"
    shared.mkdir()
    add_checkout(config.projects_dir, "a", shared)
    add_checkout(config.projects_dir, "b", shared)
    add_checkout(config.projects_dir, "a", tmp_path / "gone")
    messages = [c.message for c in doctor.check_checkouts(config)]
    assert any("does not exist" in m for m in messages) and any("owned by a, b" in m for m in messages)
    add_checkout(config.projects_dir, "b", shared, shared=True)
    messages = [c.message for c in doctor.check_checkouts(config)]
    assert not any("owned by a, b" in m for m in messages)


def test_tmux_outside_tmux_warns():
    assert levels(doctor.check_tmux({}), "tmux") in ([doctor.WARN], [doctor.FAIL])


def test_cli_exit_code(config, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GOPHER_HOME", str(config.root))
    monkeypatch.setattr(doctor, "settings_path", lambda: tmp_path / "none.json")
    assert main(["doctor", "-q"]) == 1
    assert "fail  hooks" in capsys.readouterr().out


def test_failed_task_shows_its_last_output_line(config):
    config.schedule_dir.mkdir(exist_ok=True)
    (config.schedule_dir / "watch.md").write_text("---\nevery: 1h\nrun: echo starting; echo login expired >&2; exit 1\nnotify: output\n---\nx\n")
    schedule.tick(config, 1e9)
    messages = [c.message for c in doctor.check_schedule(config, 1e9)]
    assert "watch failed (exit 1): login expired" in messages


def test_update_check_result_shows_in_doctor(config, monkeypatch):
    now = 1_800_000_000.0
    output = "claude-gopher v0.2.0 is available (installed 0.1.0); release notes: x"
    schedule.save_state(config, {"tasks": {"update-check": {"last": now - 3600, "exit": 0, "output": output}}})
    monkeypatch.setattr(doctor.update, "editable_dir", lambda: None)
    monkeypatch.setattr(doctor.update, "installed_version", lambda: "0.1.0")
    assert [c.message for c in doctor.check_update(config, now)] == ["v0.2.0 is available (installed 0.1.0); gopher update"]
    monkeypatch.setattr(doctor.update, "installed_version", lambda: "0.2.0")
    assert [(c.level, c.message) for c in doctor.check_update(config, now)] == [(doctor.OK, "0.2.0 is current (checked 1h ago)")]
