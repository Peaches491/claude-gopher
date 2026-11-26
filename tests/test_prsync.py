import json
import subprocess

import pytest

from claude_gopher import prsync, schedule
from claude_gopher.cli import main
from claude_gopher.config import load_config
from claude_gopher.inbox import unread
from claude_gopher.projects import read_frontmatter
from claude_gopher.roots import create_project, create_root

URL = "https://git.example.com/org/repo/pull/{}"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "root"
    create_root(root)
    config = load_config(root)
    for name in ("alpha", "beta"):
        create_project(config.projects_dir, name, "")
    return config


def pr(config, project, number, frontmatter, url=None):
    directory = config.projects_dir / project / "resources" / "prs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{number}.md"
    head = f"---\n{frontmatter}---\n" if frontmatter else ""
    path.write_text(f"{head}# [PR {number}]({url or URL.format(number)})\n\nWhat it does.\n")
    return path


@pytest.fixture
def fake_gh(monkeypatch):
    answers, calls = {}, []

    def run(argv, **kwargs):
        calls.append(argv)
        number = argv[3].rsplit("/", 1)[1]
        answer = answers.get(number)
        if answer is None:
            return subprocess.CompletedProcess(argv, 1, "", "GraphQL: Could not resolve to a PullRequest\n")
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")

    monkeypatch.setattr(prsync.subprocess, "run", run)
    monkeypatch.setattr(prsync.shutil, "which", lambda name: "/usr/bin/gh")
    return answers, calls


def test_sync_updates_states_and_files_one_inbox_item_per_project(config, fake_gh):
    answers, calls = fake_gh
    merged = pr(config, "alpha", 11, "state: open\nstack: 11 12\n")
    closed = pr(config, "alpha", 12, "state: draft  # waiting\n")
    same = pr(config, "beta", 21, "state: open\n")
    bare = pr(config, "beta", 22, "")
    pr(config, "beta", 23, "state: merged\nmerged: 2026-11-01\n")
    pr(config, "beta", 24, "state: open\n", url="https://example.com/not-a-pull")
    answers.update({
        "11": {"state": "MERGED", "isDraft": False, "mergedAt": "2026-11-24T22:58:00Z"},
        "12": {"state": "CLOSED", "isDraft": False, "mergedAt": None, "closedAt": "2026-11-23T10:00:00Z"},
        "21": {"state": "OPEN", "isDraft": False, "mergedAt": None},
        "22": {"state": "OPEN", "isDraft": True, "mergedAt": None},
    })
    changes, errors = prsync.sync(config)
    assert errors == []
    assert sorted(c.path.stem for c in changes) == ["11", "12", "22"]
    assert len(calls) == 4
    fields, body = read_frontmatter(merged.read_text())
    assert fields["state"] == "merged" and fields["stack"] == "11 12" and fields["merged"].startswith("2026-11-2")
    assert body.startswith("# [PR 11]")
    closed_fields = read_frontmatter(closed.read_text())[0]
    assert set(closed_fields) == {"state", "closed"} and closed_fields["state"] == "closed"
    assert closed_fields["closed"].startswith("2026-11-23T")
    assert read_frontmatter(bare.read_text())[0] == {"state": "draft"}
    assert read_frontmatter(same.read_text())[0] == {"state": "open"}
    alpha = unread(config.projects_dir / "alpha")
    assert len(alpha) == 1 and alpha[0].sender == "gopher" and not alpha[0].wake
    assert "#11 merged 2026-11-2" in alpha[0].body and "#12 closed without merging 2026-11-23 " in alpha[0].body
    assert "#22 open -> draft" in unread(config.projects_dir / "beta")[0].body


def test_dry_run_and_errors(config, fake_gh, capsys):
    answers, _ = fake_gh
    path = pr(config, "alpha", 11, "state: open\n")
    pr(config, "alpha", 13, "state: open\n")
    answers["11"] = {"state": "MERGED", "isDraft": False, "mergedAt": None}
    before = path.read_text()
    assert main(["--root", str(config.root), "prs", "sync", "--dry-run"]) == 1
    out, err = capsys.readouterr()
    assert out == "alpha: #11 merged (dry run)\n"
    assert "pull/13: GraphQL: Could not resolve" in err
    assert path.read_text() == before and unread(config.projects_dir / "alpha") == []


def test_missing_gh_is_an_error(config, monkeypatch, capsys):
    pr(config, "alpha", 11, "state: open\n")
    monkeypatch.setattr(prsync.shutil, "which", lambda name: None)
    assert main(["--root", str(config.root), "prs", "sync"]) == 1
    assert "gh" in capsys.readouterr().err


def test_new_roots_with_gh_get_an_hourly_pr_sync(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(schedule, "have_gh", lambda: True)
    create_root(tmp_path / "root")
    config = load_config(tmp_path / "root")
    task = {t.name: t for t in schedule.load_tasks(config)[0]}["pr-sync"]
    assert (task.run, task.notify, task.background) == ("gopher prs sync", "failure", True)
    assert task.timing() == "every 1h weekdays 08:00-20:00"
    assert main(["--root", str(config.root), "schedule", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "GOPHER" in lines[0]
    assert "on failure" in next(line for line in lines if line.startswith("pr-sync"))
    assert "on change" in next(line for line in lines if line.startswith("update-check"))
