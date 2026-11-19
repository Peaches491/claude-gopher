import subprocess

import pytest

from claude_gopher import rootgit
from claude_gopher.config import load_config
from claude_gopher.roots import create_project, create_root


def run(*argv, cwd):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


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
    create_project(config.projects_dir, "beta")
    remote = tmp_path / "remote.git"
    run("git", "init", "-q", "--bare", str(remote), cwd=tmp_path)
    run("git", "add", "-A", cwd=root)
    run("git", "commit", "-q", "-m", "init", cwd=root)
    run("git", "remote", "add", "origin", str(remote), cwd=root)
    run("git", "push", "-q", "-u", "origin", "HEAD", cwd=root)
    return config


def test_group_of_splits_projects_and_root(config):
    dirs = rootgit.lifecycle_dirs(config)
    assert rootgit.group_of("projects/alpha/STATUS.md", dirs) == ("projects/alpha", "STATUS.md")
    assert rootgit.group_of("archived/old/plan/x.md", dirs) == ("archived/old", "plan")
    assert rootgit.group_of("config.toml", dirs) == ("root", "config.toml")
    assert rootgit.commit_message("projects/alpha", {"STATUS.md", "human", "plan"}) == "alpha: update STATUS, human notes, plan"


def test_save_commits_per_project_and_pushes(config):
    root = config.root
    (config.projects_dir / "alpha" / "STATUS.md").write_text("changed\n")
    (config.projects_dir / "beta" / "plan").mkdir(exist_ok=True)
    (config.projects_dir / "beta" / "plan" / "p.md").write_text("plan\n")
    (root / "config.toml").write_text((root / "config.toml").read_text() + "\n# note\n")
    assert rootgit.save(config, dry_run=True).commits == ["alpha: update STATUS", "beta: update plan", "root: update config.toml"]
    assert run("git", "status", "--porcelain", cwd=root)
    report = rootgit.save(config)
    assert report.pushed and report.commits == ["alpha: update STATUS", "beta: update plan", "root: update config.toml"]
    assert run("git", "status", "--porcelain", cwd=root) == ""
    assert run("git", "log", "--format=%s", "-3", "@{upstream}", cwd=root).splitlines() == [
        "root: update config.toml", "beta: update plan", "alpha: update STATUS",
    ]
    assert run("git", "show", "--stat", "--format=", "HEAD~2", cwd=root).startswith("projects/alpha/STATUS.md |")
    assert rootgit.save(config).commits == []


def test_save_refuses_during_a_merge(config):
    git_dir = config.root / ".git"
    (git_dir / "MERGE_HEAD").write_text("abc\n")
    with pytest.raises(rootgit.SaveError, match="MERGE_HEAD"):
        rootgit.save(config)


def test_save_reports_failed_push(config, tmp_path):
    (config.projects_dir / "alpha" / "STATUS.md").write_text("changed\n")
    run("git", "remote", "set-url", "origin", str(tmp_path / "missing.git"), cwd=config.root)
    with pytest.raises(rootgit.SaveError, match="push failed"):
        rootgit.save(config)
    assert run("git", "log", "--format=%s", "-1", cwd=config.root) == "alpha: update STATUS"


def test_save_refuses_a_root_that_is_not_a_repository(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "config.toml").write_text("")
    with pytest.raises(rootgit.SaveError, match="not its own git repository"):
        rootgit.save(load_config(root))


def test_backup_status_counts_unsaved_and_unpushed(config):
    status = rootgit.backup_status(config.root)
    assert (status.changed, status.unpushed, status.error) == (0, 0, None)
    assert status.describe(status.last_commit + 300, 2) == "backup: saved 5m ago, pushed"
    status_md = config.projects_dir / "alpha" / "STATUS.md"
    old_lines = len(status_md.read_text().splitlines())
    status_md.write_text("changed\n")
    (config.projects_dir / "beta" / "new.md").write_text("one\ntwo\nthree")
    (config.projects_dir / "beta" / "blob.bin").write_bytes(b"a\0b\n")
    run("git", "commit", "-q", "--allow-empty", "-m", "local", cwd=config.root)
    status = rootgit.backup_status(config.root)
    assert (status.changed, status.unpushed) == (3, 1)
    assert (status.added, status.removed) == (4, old_lines)
    assert status.level(status.last_commit + 3600, 2) == "ok"
    late = status.last_commit + 3 * 3600
    assert status.level(late, 2) == "overdue"
    assert status.describe(late, 2) == f"backup: saved 3h ago (due every 2h), unsaved +4 -{old_lines} ~3, 1 unpushed"
    assert status.level(late, 0) == "ok"


def test_backup_status_flags_a_missing_upstream_or_repo(config, tmp_path):
    run("git", "branch", "--unset-upstream", cwd=config.root)
    status = rootgit.backup_status(config.root)
    assert status.level(0, 2) == "error" and "no upstream" in status.describe(status.last_commit, 2)
    loose = tmp_path / "loose"
    loose.mkdir()
    assert rootgit.backup_status(loose).describe(0, 2) == "backup: the root is not its own git repository"


def test_backup_compact_line():
    status = rootgit.BackupStatus(last_commit=0, changed=0, unpushed=0)
    assert status.compact(300, 2) == "\U0001f4be 5m ago \u2713"
    status = rootgit.BackupStatus(last_commit=0, changed=3, added=9, removed=1, unpushed=2)
    assert status.compact(5 * 3600, 2) == "\U0001f4be 5h ago (every 2h) (+9 -1 ~3) \u21e12"
    assert rootgit.BackupStatus(last_commit=0, unpushed=None).compact(60, 2) == "\U0001f4be 1m ago \u26a0 no upstream"
    assert rootgit.BackupStatus(error="no repo").compact(0, 2) == "\U0001f4be no repo"
