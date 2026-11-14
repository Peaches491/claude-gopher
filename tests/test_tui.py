import asyncio
import datetime
import os
import time

import pytest

from claude_gopher import overview, tui
from claude_gopher.checkouts import add_checkout
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


def test_card_text():
    beta = project("beta", unread=2, wake=1)
    beta["state"] = "waiting-review"
    assert tui.title_text(beta).plain.startswith("beta  ") and "waiting-review" not in tui.title_text(beta).plain
    assert tui.project_state_text(beta).plain == "waiting-review"
    assert tui.facts_text(beta).plain == "\u2709 2!  #602 #601"
    assert tui.body_text(beta, None).plain == "beta summary"
    assert tui.body_text(beta, "cached").plain == "~ cached"


def test_agent_pane_beside_finds_claude(monkeypatch):
    listing = "%1\tpython3\n%2\tzsh\n%3\tclaude\n"
    monkeypatch.setattr(tui, "tmux", lambda *argv: Done(listing))
    assert tui.agent_pane_beside("%1") == "%3"
    assert tui.agent_pane_beside("%3") is None
    assert tui.agent_pane_beside(None) is None


def drive_app(app, steps):
    async def run():
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await steps(app, pilot)

    asyncio.run(run())


def stub_tmux(monkeypatch):
    sent = []
    monkeypatch.setattr(tui, "tmux", lambda *argv: sent.append(argv) or Done())
    monkeypatch.setattr(tui, "focused_panes", lambda: {})
    return sent


def test_dashboard_keys(config, monkeypatch):
    rows = [project("beta", [{"state": "waiting", "pane": "%9", "updated": 1, "location": None}], unread=1, wake=1), project("alpha")]
    monkeypatch.setattr(tui, "project_overview", lambda config: rows)
    stub_tmux(monkeypatch)
    jumps, popups = [], []
    monkeypatch.setattr(tui.popup, "jump_to", jumps.append)
    monkeypatch.setattr(tui.popup, "open_popup", lambda kind, origin, target=None, prefill="", client=None: popups.append((kind, target, prefill)))
    monkeypatch.setenv("TMUX_PANE", "%1")
    app = tui.GopherTUI(config)

    async def steps(app, pilot):
        assert [card.project_name for card in app.cards()] == ["beta", "alpha"]
        assert not app.query(tui.PinnedHeader)
        await pilot.press("enter")
        assert jumps == ["%9"]
        await pilot.press("i")
        assert popups[-1] == ("agent", "%9", "")
        await pilot.press("j", "g")
        assert popups[-1] == ("gopher", None, "[project: alpha] ")
        await pilot.press("end")
        assert app.selected()["name"] == "alpha"
        await pilot.press("home")
        assert app.selected()["name"] == "beta"

    drive_app(app, steps)


def test_sidebar_pins_project_and_works_inbox(config, monkeypatch):
    alpha_dir = config.projects_dir / "alpha"
    item = tui.add_item(alpha_dir, "please look at the flaky test", sender="gopher")
    pinned = project("alpha", [{"state": "working", "pane": "%3", "updated": 2, "location": None}], unread=1)
    pinned["path"] = str(alpha_dir)
    rows = [pinned, project("beta", [{"state": "idle", "pane": "%9", "updated": 1, "location": None}])]
    monkeypatch.setattr(tui, "project_overview", lambda config: rows)
    sent = stub_tmux(monkeypatch)
    monkeypatch.setattr(tui, "agent_pane_beside", lambda own: "%3")
    popups = []
    monkeypatch.setattr(tui.popup, "open_popup", lambda kind, origin, target=None, prefill="", client=None: popups.append((kind, target, prefill)))
    monkeypatch.setenv("TMUX_PANE", "%2")
    app = tui.GopherTUI(config, pinned="alpha")

    async def steps(app, pilot):
        assert [card.project_name for card in app.cards()] == ["beta"]
        assert all(card.compact for card in app.cards())
        assert "alpha" in str(app.query_one("#pinned-text", tui.Static).render())
        await pilot.press("i")
        assert popups[-1] == ("agent", "%9", "[from: alpha] ")
        await pilot.press("b")
        await pilot.pause()
        inbox = app.query_one(tui.InboxList)
        assert inbox.has_class("open") and len(inbox) == 1
        await pilot.press("enter")
        assert ("send-keys", "-t", "%3", "-l", f"Inbox item inbox/{item.name}: ") in sent
        inbox.focus()
        await pilot.press("x")
        await pilot.pause()
        assert not item.exists() and (alpha_dir / "inbox" / "read" / item.name).exists()
        await pilot.press("escape")
        assert not inbox.has_class("open")
        await pilot.press("u")
        assert ("send-keys", "-t", "%3", "-l", tui.REFRESH_STATUS_PROMPT) in sent

    drive_app(app, steps)


ACTIONS_TOML = """
[[tui.actions]]
key = "s"
label = "Where"
command = "pwd"

[[tui.actions]]
key = "w"
label = "Who"
command = "echo $GOPHER_PROJECT"
cwd = "project"

[[tui.actions]]
key = "j"
label = "Clash"
command = "true"
"""


def write_actions(config):
    path = config.root / "config.toml"
    path.write_text(path.read_text() + ACTIONS_TOML)
    return load_config(config.root)


def test_run_custom_action_uses_checkout_or_project_dir(config, tmp_path):
    checkout = tmp_path / "co"
    checkout.mkdir()
    loaded = write_actions(config)
    where, who, _ = tui.tui_actions(loaded)
    alpha = {"name": "alpha", "path": str(config.projects_dir / "alpha")}
    with pytest.raises(RuntimeError, match="no checkouts"):
        tui.run_custom_action(loaded, alpha, where)
    add_checkout(config.projects_dir, "alpha", checkout)
    assert tui.run_custom_action(loaded, alpha, where) == str(checkout)
    assert tui.run_custom_action(loaded, alpha, who) == "alpha"


def test_tui_actions_rejects_bad_entries(config):
    path = config.root / "config.toml"
    path.write_text(path.read_text() + '\n[[tui.actions]]\nkey = "s"\nlabel = "x"\ncommand = "true"\ncwd = "home"\n')
    with pytest.raises(tui.ConfigError, match="cwd"):
        tui.tui_actions(load_config(config.root))


def test_sidebar_binds_custom_actions(config, monkeypatch):
    loaded = write_actions(config)
    pinned = project("alpha", [])
    pinned["path"] = str(config.projects_dir / "alpha")
    monkeypatch.setattr(tui, "project_overview", lambda config: [pinned])
    stub_tmux(monkeypatch)
    ran = []
    monkeypatch.setattr(tui, "run_custom_action", lambda config, project, action: ran.append(action.label) or "done")
    app = tui.GopherTUI(loaded, pinned="alpha")

    async def steps(app, pilot):
        labels = [str(button.label) for button in app.query("#pinned-buttons Button")]
        assert labels[:2] == ["s Where", "w Who"] and "j Clash" not in labels
        assert any("already taken" in problem for problem in app.action_problems)
        await pilot.press("w")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ran == ["Who"]
        assert "done" in str(app.query_one("#pinned-output", tui.Static).render())

    drive_app(app, steps)


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
