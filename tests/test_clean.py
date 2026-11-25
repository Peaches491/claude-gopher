import datetime
import shutil

from claude_gopher import clean
from claude_gopher.checkouts import add_checkout
from claude_gopher.projects import read_frontmatter
from test_lifecycle import config, repo_with_worktree, run  # noqa: F401  (shared fixtures)


def set_updated(config, name, day):
    path = config.projects_dir / name / "STATUS.md"
    fields, body = read_frontmatter(path.read_text())
    path.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in {**fields, "updated": day}.items()) + "---\n" + body)


def test_stale_projects_need_the_working_day_threshold(config, monkeypatch):
    monkeypatch.setattr(clean, "current_sessions", lambda config, prune=False: [{"project": "alpha"}])
    set_updated(config, "alpha", "2026-10-01")
    set_updated(config, "beta", "2026-11-16")
    now = datetime.datetime(2026, 11, 20, 12).timestamp()
    stale = clean.stale_projects(config, now, 15)
    assert [(p.name, p.running) for p in stale] == [("alpha", True)]


def test_orphaned_worktrees_skip_owned_and_report_state(config, tmp_path):
    main, owned = repo_with_worktree(tmp_path, "owned", "owned")
    _, orphan = repo_with_worktree(tmp_path, "orphan", "orphan")
    _, gone = repo_with_worktree(tmp_path, "gone", "gone")
    add_checkout(config.projects_dir, "alpha", owned)
    (orphan / "README").write_text("changed\n")
    run("git", "commit", "-q", "-am", "local", cwd=orphan)
    (orphan / "README").write_text("dirty\n")
    shutil.rmtree(gone)
    found = {o.path.name: o for o in clean.orphaned_worktrees(config)}
    assert set(found) == {"orphan", "gone"}
    assert found["orphan"].branch == "orphan"
    assert found["orphan"].blockers == ["uncommitted changes (1 files)", "1 commit(s) not on any remote branch"]
    assert found["gone"].missing
    lines = clean.render(clean.Report([], list(found.values()), []), 0, 15, clean.LARGE_BYTES)
    assert any("worktree prune" in line for line in lines)


def test_tmp_dirs_flag_large_and_ownerless(config):
    tmp = config.root / "tmp"
    for name, size in (("alpha", 2 * 1024 * 1024), ("beta", 10), ("old-project", 10), ("gopher", 10)):
        (tmp / name).mkdir(parents=True, exist_ok=True)
        (tmp / name / "data").write_bytes(b"x" * size)
    found = {t.path.name: t.reason for t in clean.tmp_dirs(config, 1024 * 1024)}
    assert found == {"alpha": "large", "old-project": "no open project by that name"}


def test_nothing_to_clean(config):
    assert clean.render(clean.Report([], [], []), 0, 15, clean.LARGE_BYTES) == ["Nothing to clean up."]
