import asyncio
import datetime
import os
import time

import pytest

from claude_gopher import overview, tui
from claude_gopher.config import load_config
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
    config = load_config(root)
    create_project(config.projects_dir, "alpha", "First")
    create_project(config.projects_dir, "beta", "Second")
    return config


def project(name, sessions=(), unread=0, wake=0):
    return {
        "name": name, "path": f"/p/{name}", "summary": f"{name} summary", "state": "active", "updated": "2026-11-10",
        "sessions": list(sessions), "checkouts": [], "resources": {"prs": 2, "tickets": 0},
        "links": {"prs": [overview.Resource("602", "https://forge/pull/602"), overview.Resource("601", None)], "tickets": []},
        "inbox": {"unread": unread, "wake": wake, "items": []},
    }


@pytest.mark.parametrize("updated, expected", [
    (None, "-"), ("2026-11-12", "today"), ("2026-11-09", "3d"), ("soon", "soon"),
])
def test_updated_age(updated, expected):
    assert tui.updated_age(updated, datetime.date(2026, 11, 12)) == expected


def test_agents_text_shows_most_urgent_only():
    sessions = [
        {"state": "idle", "location": {"session": "w", "window_index": "2", "pane_index": "1", "window_name": "x"}},
        {"state": "waiting", "location": None},
    ]
    assert tui.agents_text(sessions).plain == "\u25cf needs input +1"
    assert tui.agents_text(sessions[:1]).plain == "\u25cb idle"
    assert tui.agents_text([]).plain == "no agent"


def test_inbox_text():
    assert tui.inbox_text({"unread": 0, "wake": 0}).plain == ""
    assert tui.inbox_text({"unread": 2, "wake": 1}).plain == "\u2709 2!"


def test_jump_target_prefers_waiting_then_recent():
    sessions = [
        {"pane": "%1", "state": "idle", "updated": 5},
        {"pane": "%2", "state": "waiting", "updated": 1},
        {"pane": None, "state": "waiting", "updated": 9},
    ]
    assert tui.jump_target(sessions) == "%2"
    assert tui.jump_target([{"pane": "%1", "state": "idle", "updated": 1}, {"pane": "%3", "state": "idle", "updated": 2}]) == "%3"
    assert tui.jump_target([]) is None


def test_summary_cache_expires_when_status_changes(config):
    cache = tui.SummaryCache(config)
    entry = {"name": "alpha", "path": str(config.projects_dir / "alpha")}
    assert cache.get(entry) is None
    cache.put("alpha", "doing things")
    assert cache.get(entry) == "doing things"
    status = config.projects_dir / "alpha" / "STATUS.md"
    later = time.time() + 10
    os.utime(status, (later, later))
    assert cache.get(entry) is None


def test_summary_input_includes_both_statuses(config):
    (config.projects_dir / "alpha" / "human").mkdir(exist_ok=True)
    (config.projects_dir / "alpha" / "human" / "STATUS.md").write_text("my notes")
    text = tui.summary_input(config.projects_dir / "alpha")
    assert "<STATUS.md>" in text and "<human/STATUS.md>\nmy notes" in text


def test_gopher_pane_skips_self_and_shells(monkeypatch):
    listing = "%1\tpython3\n%2\tzsh\n%3\tclaude\n"
    monkeypatch.setattr(tui, "tmux", lambda *argv: type("R", (), {"returncode": 0, "stdout": listing})())
    assert tui.gopher_pane("%1") == "%3"
    assert tui.gopher_pane(None) is None


def test_card_text():
    beta = project("beta", unread=2, wake=1)
    beta["state"] = "waiting-review"
    assert tui.title_text(beta).plain.startswith("beta  ") and "waiting-review" not in tui.title_text(beta).plain
    assert tui.project_state_text(beta).plain == "waiting-review"
    assert tui.facts_text(beta).plain == "\u2709 2!  #602 #601"
    assert tui.body_text(beta, None).plain == "beta summary"
    assert tui.body_text(beta, "cached").plain == "~ cached"


def test_app_shows_projects_and_addresses_gopher(config, monkeypatch):
    rows = [project("beta", [{"state": "waiting", "pane": "%9", "updated": 1, "location": None}], unread=1, wake=1), project("alpha")]
    monkeypatch.setattr(tui, "project_overview", lambda config: rows)
    sent = []
    monkeypatch.setattr(tui, "gopher_pane", lambda own: "%5")
    monkeypatch.setattr(tui, "tmux", lambda *argv: sent.append(argv))
    monkeypatch.setattr(tui, "in_gopher_window", lambda own: True)
    monkeypatch.setattr(tui, "focused_panes", lambda: {})
    monkeypatch.setattr(tui, "popup_clients", lambda: (None, []))
    origins = iter([None, "%9"])
    monkeypatch.setattr(tui, "popup_origin", lambda: next(origins, "%9"))
    app = tui.GopherTUI(config)

    async def drive():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert [card.project_name for card in app.cards()] == ["beta", "alpha"]
            assert app.selected()["name"] == "beta"
            await pilot.press("j")
            assert app.selected()["name"] == "alpha"
            await pilot.press("i")
            assert ("send-keys", "-t", "%5", "-l", "[project: alpha] ") in sent
            await pilot.press("k", "t")
            assert ("switch-client", "-t", "%9") in sent
            await pilot.press("G")
            assert app.selected()["name"] == "alpha"
            await pilot.press("g")
            assert app.selected()["name"] == "beta"
            await pilot.press("j")
            app.origin = None
            app.check_origin()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.selected()["name"] == "beta"

    asyncio.run(drive())


def test_project_rank_tiers_then_recency():
    waiting_old = project("w-old", [{"state": "waiting", "updated": 1}])
    waiting_new = project("w-new", [{"state": "waiting", "updated": 5}])
    working = project("work", [{"state": "idle", "updated": 9}, {"state": "working", "updated": 2}])
    background = project("bg", [{"state": "background", "updated": 3}])
    idle_recent = project("i-recent", [{"state": "idle", "updated": 8}])
    idle_stale = project("i-stale", [{"state": "idle", "updated": 4}])
    none_new = project("n-new")
    none_old = project("n-old")
    none_old["updated"] = "2026-01-01"
    ranked = sorted([none_old, idle_stale, none_new, working, waiting_new, idle_recent, background, waiting_old], key=overview.project_rank)
    assert [p["name"] for p in ranked] == ["w-old", "w-new", "work", "bg", "i-recent", "i-stale", "n-new", "n-old"]


def test_links_text_collapses_stacks_and_hides_closed():
    stack = ("10", "11", "12")
    now = 1_000_000.0
    links = {
        "prs": [
            overview.Resource("12", "https://forge/pull/12", stack=stack),
            overview.Resource("11", "https://forge/pull/11", stack=stack),
            overview.Resource("10", None, state="merged", merged=now - 3600, stack=stack),
            overview.Resource("9", "https://forge/pull/9", state="merged", merged=now - 3600),
            overview.Resource("8", None, state="merged", merged=now - 90_000),
            overview.Resource("7", None, state="closed"),
        ],
        "tickets": [overview.Resource("ABC-1", "https://jira/ABC-1"), overview.Resource("ABC-2", None, state="resolved")],
    }
    text = tui.links_text(links, now)
    assert text.plain == "#12(3) \u2714#9 ABC-1"
    assert [span.style.link for span in text.spans if getattr(span.style, "link", None)] == [
        "https://forge/pull/12", "https://forge/pull/9", "https://jira/ABC-1",
    ]


def test_resource_links_reads_frontmatter(config):
    prs = config.projects_dir / "alpha" / "resources" / "prs"
    prs.mkdir(parents=True)
    (prs / "601.md").write_text("---\nstate: merged\nmerged: 2026-11-12\nstack: #600, #601\n---\n# [Fix it](https://forge/pull/601)\n\nbody\n")
    (prs / "602.md").write_text("# no link\n")
    links = overview.resource_links(config.projects_dir / "alpha")["prs"]
    assert [(r.name, r.url, r.state, r.stack) for r in links] == [
        ("602", None, "open", ()), ("601", "https://forge/pull/601", "merged", ("600", "601")),
    ]
    assert links[1].merged == overview.parse_when("2026-11-13T00:00")


def test_attention_done_until_acked_and_creds():
    session = {"session_id": "s", "state": "idle", "finished": 10.0, "location": {"blocked_creds": False}}
    assert overview.attention(session, {}) == overview.DONE
    assert overview.attention(session, {"s": 11.0}) is None
    assert overview.attention({**session, "state": "working"}, {}) is None
    assert overview.attention({**session, "location": {"blocked_creds": True}}, {"s": 11.0}) == overview.CREDS


def test_focus_tracker_needs_dwell_after_finish():
    tracker = tui.FocusTracker(dwell=5)
    session = {"session_id": "s", "pane": "%1", "attention": overview.DONE, "finished": 100.0}
    since = tracker.observe({"c": "%1"}, 90.0)
    assert tracker.seen([session], since, 90.0) == []
    since = tracker.observe({"c": "%1"}, 103.0)
    assert tracker.seen([session], since, 103.0) == []
    since = tracker.observe({"c": "%1"}, 106.0)
    assert tracker.seen([session], since, 106.0) == ["s"]
    since = tracker.observe({"c": "%2"}, 107.0)
    since = tracker.observe({"c": "%1"}, 108.0)
    assert tracker.seen([session], since, 110.0) == []


class Done:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode


def test_jump_from_popup_switches_outer_client_and_closes_popup(monkeypatch):
    calls = []
    replies = {"show": Done("/dev/pts/26\n"), "list-clients": Done("/dev/pts/26\t0\n/dev/pts/90\tgopher\n")}
    monkeypatch.setattr(tui, "tmux", lambda *argv: calls.append(argv) or replies.get(argv[0], Done()))
    tui.jump_to("%9")
    assert ("switch-client", "-c", "/dev/pts/26", "-t", "%9") in calls
    assert ("detach-client", "-t", "/dev/pts/90") in calls


def test_jump_without_popup_switches_current_client(monkeypatch):
    calls = []
    monkeypatch.setattr(tui, "tmux", lambda *argv: calls.append(argv) or (Done("", 1) if argv[0] == "show" else Done()))
    tui.jump_to("%9")
    assert calls[-1] == ("switch-client", "-t", "%9")


def test_next_level_cycles():
    assert [tui.next_level(level) for level in ("quiet", "nudge", "proactive")] == ["nudge", "proactive", "quiet"]


def test_needs_input_outranks_older_done():
    done = project("done-old", [{"state": "idle", "attention": "done", "updated": 1}])
    waiting = project("waiting", [{"state": "working", "updated": 9}, {"state": "waiting", "updated": 50}])
    assert [p["name"] for p in sorted([done, waiting], key=overview.project_rank)] == ["waiting", "done-old"]


def test_state_class_and_colour():
    beta = project("beta")
    assert tui.state_class(beta) == "pstate-active"
    beta["state"] = "waiting-review"
    assert tui.state_class(beta) == "pstate-waiting-review"
    assert tui.project_state_text(beta).spans[0].style == "yellow"
    beta["state"] = "weird"
    assert tui.state_class(beta) == "pstate-other"


def test_rank_follows_colour_groups():
    review = project("review")
    review["state"] = "waiting-review"
    blocked = project("blocked")
    blocked["state"] = "blocked"
    closing = project("closing")
    closing["state"] = "closing"
    needs = project("needs", [{"state": "waiting", "updated": 5}])
    active = project("active", [{"state": "idle", "updated": 9}])
    ranked = sorted([closing, active, blocked, review, needs], key=overview.project_rank)
    assert [p["name"] for p in ranked] == ["needs", "review", "blocked", "active", "closing"]


def test_busy_agent_does_not_lift_grey_status():
    closing = project("closing", [{"state": "working", "updated": 9}])
    closing["state"] = "closing"
    active = project("active")
    assert [p["name"] for p in sorted([closing, active], key=overview.project_rank)] == ["active", "closing"]


def test_pane_project_prefers_agent_then_cwd_then_window(config, monkeypatch):
    projects = {"alpha": project("alpha", [{"pane": "%1"}]), "beta": project("beta")}
    replies = {
        "%2": Done(f"{config.projects_dir / 'beta' / 'notes'}\tshell\n"),
        "%3": Done("/elsewhere\t\u25cb beta\n"),
        "%4": Done("/elsewhere\tnothing\n"),
    }
    monkeypatch.setattr(tui, "tmux", lambda *argv: replies[argv[3]])
    assert tui.pane_project(config, "%1", projects) == "alpha"
    assert tui.pane_project(config, "%2", projects) == "beta"
    assert tui.pane_project(config, "%3", projects) == "beta"
    assert tui.pane_project(config, "%4", projects) is None
