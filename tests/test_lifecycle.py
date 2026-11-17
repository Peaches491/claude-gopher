import subprocess

import pytest

from claude_gopher import lifecycle
from claude_gopher.checkouts import add_checkout
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
    monkeypatch.setattr(lifecycle.restore, "find_window", lambda project: None)
    root = tmp_path / "root"
    create_root(root)
    protected = tmp_path / "main"
    (root / "config.toml").write_text(
        (root / "config.toml").read_text()
        + f'\n[checkouts]\nprotected = ["{protected}"]\n\n[lifecycle]\non_archive = ["touch archived.marker"]\non_close = ["touch ../$(basename $PWD).closed"]\non_remove = ["echo $GOPHER_PROJECT $GOPHER_CHECKOUT > ../removed.log"]\n'
    )
    config = load_config(root)
    for name in ("alpha", "beta"):
        create_project(config.projects_dir, name)
    return config


def repo_with_worktree(tmp_path, name="wt", branch="feature"):
    main = tmp_path / "main"
    if not main.exists():
        remote = tmp_path / "remote.git"
        run("git", "init", "-q", "--bare", str(remote), cwd=tmp_path)
        run("git", "clone", "-q", str(remote), str(main), cwd=tmp_path)
        (main / "README").write_text("hi\n")
        run("git", "add", "README", cwd=main)
        run("git", "commit", "-q", "-m", "init", cwd=main)
        run("git", "push", "-q", "origin", "HEAD", cwd=main)
    worktree = tmp_path / name
    run("git", "worktree", "add", "-q", "-b", branch, str(worktree), cwd=main)
    return main, worktree


def test_close_plan_classifies_checkouts(config, tmp_path):
    main, worktree = repo_with_worktree(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    clone = tmp_path / "clone"
    run("git", "init", "-q", str(clone), cwd=tmp_path)
    for path in (main, worktree, shared, clone, tmp_path / "gone"):
        add_checkout(config.projects_dir, "alpha", path)
    add_checkout(config.projects_dir, "beta", shared)
    plan = lifecycle.make_plan(config, "close", "alpha")
    by_path = {c.path: c for c in plan.checkouts}
    assert by_path[main].disposition == "keep" and by_path[main].reason == "protected"
    assert by_path[shared].reason == "also owned by beta"
    assert by_path[clone].disposition == "clean" and "not a git worktree" in by_path[clone].reason
    assert by_path[tmp_path / "gone"].disposition == "skip"
    assert by_path[worktree].disposition == "remove" and by_path[worktree].branch == "feature"
    assert not plan.blockers
    assert plan.destination == config.closed_dir / "alpha"
    text = lifecycle.describe(plan)
    assert "git worktree remove, then git branch -d feature" in text and "runs: touch ../$(basename $PWD).closed" in text


def test_close_blocks_dirty_or_unpushed_worktrees(config, tmp_path):
    _, worktree = repo_with_worktree(tmp_path)
    add_checkout(config.projects_dir, "alpha", worktree)
    (worktree / "new.txt").write_text("x\n")
    run("git", "add", "new.txt", cwd=worktree)
    run("git", "commit", "-q", "-m", "local only", cwd=worktree)
    (worktree / "dirty.txt").write_text("x\n")
    plan = lifecycle.make_plan(config, "close", "alpha")
    assert any("uncommitted changes" in b for b in plan.blockers)
    assert any("1 commit(s) not on any remote branch" in b for b in plan.blockers)
    with pytest.raises(lifecycle.LifecycleError, match="blocked"):
        lifecycle.execute(config, plan)
    assert (config.projects_dir / "alpha").is_dir()


def test_close_removes_worktree_and_moves_project(config, tmp_path):
    main, worktree = repo_with_worktree(tmp_path)
    add_checkout(config.projects_dir, "alpha", worktree)
    add_checkout(config.projects_dir, "alpha", main)
    report = lifecycle.execute(config, lifecycle.make_plan(config, "close", "alpha"))
    assert not worktree.exists() and main.is_dir()
    assert (tmp_path / "wt.closed").exists() and not (tmp_path / "main.closed").exists()
    assert (tmp_path / "removed.log").read_text().strip() == f"alpha {worktree}"
    assert "deleted branch feature" in report
    assert "feature" not in run("git", "branch", "--list", cwd=main)
    assert (config.closed_dir / "alpha" / "STATUS.md").is_file() and not (config.projects_dir / "alpha").exists()


def test_archive_keeps_checkouts_and_reopen_moves_back(config, tmp_path):
    _, worktree = repo_with_worktree(tmp_path)
    add_checkout(config.projects_dir, "alpha", worktree)
    plan = lifecycle.make_plan(config, "archive", "alpha")
    assert [c.disposition for c in plan.checkouts] == ["clean"]
    lifecycle.execute(config, plan)
    assert worktree.is_dir() and (worktree / "archived.marker").exists()
    assert (config.archived_dir / "alpha").is_dir()
    with pytest.raises(lifecycle.LifecycleError, match="no open project"):
        lifecycle.make_plan(config, "archive", "alpha")
    reopen = lifecycle.make_plan(config, "reopen", "alpha")
    assert reopen.commands == [] and reopen.source == config.archived_dir / "alpha"
    lifecycle.execute(config, reopen)
    assert (config.projects_dir / "alpha").is_dir() and not (config.archived_dir / "alpha").exists()


def test_execute_closes_the_open_window_first(config, tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle.restore, "find_window", lambda project: "@4")
    closed = []
    monkeypatch.setattr(lifecycle.restore, "close_window", lambda config, project: closed.append(project))
    plan = lifecycle.make_plan(config, "archive", "beta")
    assert "window @4 is snapshotted and closed" in lifecycle.describe(plan)
    assert lifecycle.execute(config, plan)[0] == "window snapshotted and closed"
    assert closed == ["beta"]


def test_close_keeps_a_worktree_git_refuses_to_remove(config, tmp_path):
    _, worktree = repo_with_worktree(tmp_path)
    add_checkout(config.projects_dir, "alpha", worktree)
    plan = lifecycle.make_plan(config, "close", "alpha")
    (worktree / "late.txt").write_text("appeared after planning\n")
    report = lifecycle.execute(config, plan)
    assert worktree.is_dir() and any(line.startswith(f"kept {worktree}") for line in report)
