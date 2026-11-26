import asyncio
import datetime
import os
import subprocess
import time
from pathlib import Path

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
        assert [card.styles.height.value for card in app.cards()] == [4]
        assert app.cards()[0].query_one(".body", tui.Static)
        header = app.query_one(tui.PinnedHeader)
        assert "alpha" in str(header.query_one(".name", tui.Static).render())
        assert header.has_class("pstate-active")
        await pilot.press("i")
        assert popups[-1] == ("agent", "%9", "[from: alpha] ")
        header.query(tui.Button).first().focus()
        await pilot.press("g")
        assert popups[-1] == ("gopher", None, "[project: alpha] ")
        app.project_list().focus()
        await pilot.press("g")
        assert popups[-1] == ("gopher", None, "[project: beta] ")
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
        closed = []
        monkeypatch.setattr(tui.restore, "close_window", lambda config, name: closed.append(name))
        await pilot.press("Q")
        await pilot.pause()
        assert isinstance(app.screen, tui.ConfirmScreen)
        await pilot.press("y", "enter")
        assert not closed
        await pilot.press("Q", "y", "e", "s", "enter")
        await app.workers.wait_for_complete()
        assert closed == ["alpha"]

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

[[tui.actions]]
key = "z"
label = "Scoped"
command = "pwd"
match = "*/scoped-*"
"""


def write_actions(config):
    path = config.root / "config.toml"
    path.write_text(path.read_text() + ACTIONS_TOML)
    return load_config(config.root)


def run_action(loaded, project, action):
    cwd, env = tui.action_context(loaded, project, action)
    result = tui.subprocess.run(action.command, shell=True, cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True)
    return result.stdout.strip()


def test_action_context_uses_checkout_or_project_dir(config, tmp_path):
    checkout = tmp_path / "co"
    checkout.mkdir()
    loaded = write_actions(config)
    where, who, _, scoped = tui.tui_actions(loaded)
    alpha = {"name": "alpha", "path": str(config.projects_dir / "alpha")}
    with pytest.raises(RuntimeError, match="no checkouts"):
        tui.action_context(loaded, alpha, where)
    add_checkout(config.projects_dir, "alpha", checkout)
    assert run_action(loaded, alpha, where) == str(checkout)
    assert run_action(loaded, alpha, who) == "alpha"
    assert not tui.action_applies(alpha, scoped)
    with pytest.raises(RuntimeError, match="no checkout matching"):
        tui.action_context(loaded, alpha, scoped)
    other = tmp_path / "scoped-co"
    other.mkdir()
    add_checkout(config.projects_dir, "alpha", other)
    assert tui.action_applies(alpha, scoped)
    assert run_action(loaded, alpha, scoped) == str(other)


def test_summary_lines_setting(config):
    assert tui.sidebar_summary_lines(config) == 2
    path = config.root / "config.toml"
    path.write_text(path.read_text() + "\n[tui]\nsummary_lines = 0\n")
    assert tui.sidebar_summary_lines(load_config(config.root)) == 0
    hidden = tui.ProjectCard(project("alpha", []), None, compact=True, summary_lines=0)
    assert "body" not in hidden.parts()
    path.write_text(path.read_text().replace("summary_lines = 0", "summary_lines = -1"))
    with pytest.raises(tui.ConfigError, match="summary_lines"):
        tui.sidebar_summary_lines(load_config(config.root))


def test_seen_dwell_setting(config):
    assert tui.GopherTUI(config).focus.dwell == 5
    path = config.root / "config.toml"
    path.write_text(path.read_text() + "\n[tui]\nseen_dwell_seconds = 30\n")
    assert tui.GopherTUI(load_config(config.root)).focus.dwell == 30
    path.write_text(path.read_text().replace("= 30", '= "soon"'))
    app = tui.GopherTUI(load_config(config.root))
    assert app.focus.dwell == 5 and any("seen_dwell_seconds" in p for p in app.action_problems)


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
    monkeypatch.setenv("TMUX_PANE", "%2")
    checkout = config.root / "co"
    checkout.mkdir()
    add_checkout(config.projects_dir, "alpha", checkout)
    monkeypatch.setattr(tui.popup, "command_popup", lambda origin, cwd, command, env, title="": ran.append((origin, cwd, command, env["GOPHER_PROJECT"])))
    app = tui.GopherTUI(loaded, pinned="alpha")

    async def steps(app, pilot):
        await pilot.pause()
        labels = [str(button.label) for button in app.query("#pinned-buttons Button") if button.display]
        assert labels[:2] == ["s Where", "w Who"] and "j Clash" not in labels and "z Scoped" not in labels
        await pilot.press("z")
        await app.workers.wait_for_complete()
        assert ran == []
        assert any("already taken" in problem for problem in app.action_problems)
        buttons = [button for button in app.query("#pinned-buttons Button") if button.display]
        buttons[0].focus()
        await pilot.pause()
        hint = app.query_one("#button-hint", tui.Static)
        assert hint.has_class("open") and "Run `pwd` in a popup" in str(hint.render())
        await pilot.press("l")
        assert app.focused is buttons[1]
        note = next(b for b in buttons if str(b.label) == "n Note")
        note.focus()
        await pilot.pause()
        assert "new inbox item" in str(hint.render())
        buttons[1].focus()
        await pilot.press("left", "h")
        assert app.focused is buttons[-1]
        await pilot.press("j")
        assert app.focused is app.project_list()
        await pilot.pause()
        assert not hint.has_class("open")
        await pilot.press("k")
        assert app.focused is buttons[0]
        await pilot.press("down")
        await pilot.press("up")
        assert app.focused is buttons[0]
        await pilot.press("k", "up")
        assert app.focused is buttons[0]
        await pilot.press("j")
        await pilot.press("w")
        assert ran == [("%2", config.projects_dir / "alpha", "echo $GOPHER_PROJECT", "alpha")]

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
            overview.Resource("6", "https://forge/pull/6", state="closed", closed_at=now - 3600),
            overview.Resource("5", None, state="closed", closed_at=now - 90_000),
        ],
        "tickets": [overview.Resource("ABC-1", "https://jira/ABC-1"), overview.Resource("ABC-2", None, state="resolved")],
    }
    text = tui.links_text(links, now)
    assert text.plain == "#12(3) \u2714#9 #6 ABC-1"
    assert [span.style.link for span in text.spans if getattr(span.style, "link", None)] == [
        "https://forge/pull/12", "https://forge/pull/9", "https://forge/pull/6", "https://jira/ABC-1",
    ]
    struck = [text.plain[span.start:span.end] for span in text.spans if getattr(span.style, "strike", False)]
    assert struck == ["#6"]


def test_resource_links_reads_frontmatter(config):
    prs = config.projects_dir / "alpha" / "resources" / "prs"
    prs.mkdir(parents=True)
    (prs / "601.md").write_text("---\nstate: merged\nmerged: 2026-11-12\nstack: #600, #601\n---\n# [Fix it](https://forge/pull/601)\n\nbody\n")
    (prs / "602.md").write_text("# no link\n")
    (prs / "603.md").write_text("---\nstate: closed  # abandoned\nclosed: 2026-11-14T09:30\n---\n# [Old](https://forge/pull/603)\n")
    links = overview.resource_links(config.projects_dir / "alpha")["prs"]
    assert [(r.name, r.url, r.state, r.stack) for r in links] == [
        ("603", "https://forge/pull/603", "closed", ()),
        ("602", None, "open", ()), ("601", "https://forge/pull/601", "merged", ("600", "601")),
    ]
    assert links[0].closed_at == overview.parse_when("2026-11-14T09:30")
    assert links[2].merged == overview.parse_when("2026-11-13T00:00")


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


def test_open_and_close_project_windows(config, monkeypatch):
    rows = [project("alpha"), project("beta")]
    monkeypatch.setattr(tui, "project_overview", lambda config: rows)
    stub_tmux(monkeypatch)
    open_windows = {"beta": "@2"}
    monkeypatch.setattr(tui, "project_window_ids", lambda name: [(open_windows[name], name)] if name in open_windows else [])
    jumps, opened, closed = [], [], []
    monkeypatch.setattr(tui.popup, "jump_to", jumps.append)
    monkeypatch.setattr(tui.restore, "open_window", lambda config, name, session=None: opened.append(name) or ("@7", tui.restore.WindowSnapshot(name, "0", "", 0, "fresh")))
    monkeypatch.setattr(tui.restore, "close_window", lambda config, name: closed.append(name))
    app = tui.GopherTUI(config)

    async def steps(app, pilot):
        assert app.selected()["name"] == "alpha"
        await pilot.press("o")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert opened == ["alpha"] and jumps == ["@7"]
        await pilot.press("j", "o")
        assert jumps[-1] == "@2" and opened == ["alpha"]
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, tui.ConfirmScreen)
        await pilot.press("escape")
        await pilot.press("c", "y", "e", "s", "enter")
        await app.workers.wait_for_complete()
        assert closed == ["beta"]

    drive_app(app, steps)


def test_priority_round_trips_in_human_status(config):
    alpha = config.projects_dir / "alpha"
    human = alpha / "human" / "STATUS.md"
    human.parent.mkdir(parents=True, exist_ok=True)
    human.write_text("## 2026-11-16\n\nmy notes\n")
    assert tui.write_priority(alpha, 2) == 2
    assert human.read_text() == "---\npriority: 2\n---\n## 2026-11-16\n\nmy notes\n"
    assert tui.write_priority(alpha, 9) == 3
    assert tui.write_priority(alpha, 0) == 0
    assert human.read_text() == "## 2026-11-16\n\nmy notes\n"


def test_priority_section_and_keys(config, monkeypatch):
    starred = {**project("beta"), "priority": 2, "path": str(config.projects_dir / "alpha")}
    rows = [starred, project("alpha"), project("gamma")]
    monkeypatch.setattr(tui, "project_overview", lambda config: rows)
    stub_tmux(monkeypatch)
    written = []
    monkeypatch.setattr(tui, "write_priority", lambda path, value: written.append(value) or value)
    app = tui.GopherTUI(config)

    async def steps(app, pilot):
        labels = [node.label if isinstance(node, tui.SectionLabel) else node.project_name for node in app.project_list().children]
        assert labels == ["Priority", "beta", "Projects", "alpha", "gamma"]
        assert app.selected()["name"] == "beta"
        assert str(app.cards()[0].query_one(".name", tui.Static).render()).startswith(tui.PRIORITY_ICON * 2)
        await pilot.press("j")
        assert app.selected()["name"] == "alpha"
        await pilot.press("plus")
        await pilot.press("k", "minus")
        assert written == [1, 1]

    drive_app(app, steps)


def test_rank_puts_stars_first():
    rows = [project("calm"), {**project("starred"), "priority": 1}, {**project("top"), "priority": 3}]
    assert [p["name"] for p in sorted(rows, key=overview.project_rank)] == ["top", "starred", "calm"]


@pytest.mark.parametrize("pattern, name, expected", [
    ("", "wfe-view", True), ("wv", "wfe-view", True), ("ew", "wfe-view", True), ("xw", "wfe-view", False),
    ("WFE", "wfe-view", True), ("wfe*", "wfe-view", True), ("*view", "wfe-view", True),
    ("pipe*", "wfe-pipedream-deploy", True), ("view*", "atlas-app", False),
])
def test_fuzzy_match(pattern, name, expected):
    assert tui.fuzzy_match(pattern, name) is expected


def test_search_filters_and_clears(config, monkeypatch):
    rows = [project("wfe-view"), project("atlas-app"), project("wfe")]
    monkeypatch.setattr(tui, "project_overview", lambda config: rows)
    stub_tmux(monkeypatch)
    jumps = []
    monkeypatch.setattr(tui.popup, "jump_to", jumps.append)
    monkeypatch.setattr(tui, "project_window_ids", lambda name: [("@1", name)])
    app = tui.GopherTUI(config)

    async def steps(app, pilot):
        await pilot.press("slash", "w", "f", "e", "j")
        await pilot.pause()
        assert app.search == "wfej" and [c.project_name for c in app.cards()] == []
        await pilot.press("backspace")
        await pilot.pause()
        assert [c.project_name for c in app.cards()] == ["wfe-view", "wfe"]
        await pilot.press("enter")
        assert app.focused is app.project_list()
        await pilot.press("j")
        assert app.selected()["name"] == "wfe"
        await pilot.press("escape")
        await pilot.pause()
        assert app.search == "" and len(app.cards()) == 3
        await pilot.press("slash", "a", "t", "enter")
        await pilot.pause()
        assert [c.project_name for c in app.cards()] == ["atlas-app"]
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert jumps == ["@1"] and app.search == "" and len(app.cards()) == 3
        assert not app.search_box().has_class("open")

    drive_app(app, steps)


def test_dev_reload_bounces_only_on_a_new_token(config, monkeypatch):
    monkeypatch.setattr(tui, "project_overview", lambda config: [project("alpha")])
    stub_tmux(monkeypatch)
    app = tui.GopherTUI(config)

    async def steps(app, pilot):
        assert not app.workers or all(w.group != "dev-reload" for w in app.workers)
        app.reload_signalled({"token": app.token})
        assert app.pending_reload is None
        app.reload_signalled({"token": "new"})
        await pilot.press("slash")
        app.reload_if_pending()
        assert not app.reloading
        await pilot.press("escape")
        await pilot.pause()
        monkeypatch.setattr(app, "exit", lambda *args, **kwargs: None)
        app.reload_if_pending()
        assert app.reloading

    drive_app(app, steps)

def test_lifecycle_buttons_plan_confirm_and_run(config, monkeypatch):
    pinned = {**project("alpha"), "path": str(config.projects_dir / "alpha")}
    monkeypatch.setattr(tui, "project_overview", lambda config: [pinned])
    stub_tmux(monkeypatch)
    plan = tui.lifecycle.Plan("archive", "alpha", config.projects_dir / "alpha", config.archived_dir / "alpha", None, [])
    monkeypatch.setattr(tui.lifecycle, "make_plan", lambda config, action, name: plan)
    ran = []
    monkeypatch.setattr(tui.lifecycle, "execute", lambda config, p: ran.append(p.action) or ["moved"])
    app = tui.GopherTUI(config, pinned="alpha")

    async def steps(app, pilot):
        labels = [str(b.label) for b in app.query("#pinned-buttons Button")]
        assert ". More" in labels and "A Archive" not in labels and "Q Close tab" not in labels
        more = next(b for b in app.query("#pinned-buttons Button") if str(b.label) == ". More")
        more.focus()
        await pilot.pause()
        assert "archive the project" in str(app.query_one("#button-hint", tui.Static).render())
        await pilot.press("full_stop")
        await pilot.pause()
        assert isinstance(app.screen, tui.MoreScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, tui.MoreScreen)
        await pilot.press("full_stop")
        await pilot.pause()
        await pilot.press("j", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, tui.ConfirmScreen) and "Archive alpha" in app.screen.question
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("full_stop", "A")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, tui.ConfirmScreen) and "Archive alpha" in app.screen.question
        await pilot.press("n", "o", "enter")
        await pilot.pause()
        assert ran == []
        await pilot.press("full_stop", "A")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("y", "e", "s", "enter")
        await app.workers.wait_for_complete()
        assert ran == ["archive"]

    drive_app(app, steps)


def test_more_menu_on_a_list_entry_targets_that_project(config, monkeypatch):
    rows = [{**project(name), "path": str(config.projects_dir / name)} for name in ("alpha", "beta")]
    monkeypatch.setattr(tui, "project_overview", lambda config: rows)
    stub_tmux(monkeypatch)
    planned = []

    def make_plan(config, action, name):
        planned.append((action, name))
        return tui.lifecycle.Plan(action, name, config.projects_dir / name, config.closed_dir / name, None, [])

    monkeypatch.setattr(tui.lifecycle, "make_plan", make_plan)
    app = tui.GopherTUI(config, pinned="alpha")

    async def steps(app, pilot):
        app.project_list().focus()
        await pilot.pause()
        assert app.selected()["name"] == "beta"
        await pilot.press("A")
        await pilot.pause()
        assert planned == [] and not isinstance(app.screen, tui.ConfirmScreen)
        await pilot.press("full_stop")
        await pilot.pause()
        assert isinstance(app.screen, tui.MoreScreen) and app.screen.project_name == "beta"
        assert [label for _, label, _, _ in app.screen.actions] == ["Close window", "Archive", "Close project"]
        app.project_list().index = None
        await pilot.press("X")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert planned == [("close", "beta")]
        assert isinstance(app.screen, tui.ConfirmScreen) and "Close beta" in app.screen.question

    drive_app(app, steps)


def test_checkouts_text_marks_agent_and_missing(tmp_path):
    here, gone = tmp_path / "here", tmp_path / "gone"
    here.mkdir()
    row = {**project("alpha", [{"state": "idle", "cwd": str(here / "src")}]), "checkouts": [str(here), str(gone)]}
    text = tui.checkouts_text(row).plain
    assert text.startswith("\ncheckouts")
    assert f"\u25b8 {here}" in text and f"  {gone}  missing" in text
    assert tui.checkouts_text({**row, "checkouts": []}).plain == ""
    assert tui.home_relative(str(Path.home() / "x")) == "~/x"


def test_idle_returns_focus_to_header(config, monkeypatch):
    pinned = {**project("alpha"), "path": str(config.projects_dir / "alpha")}
    monkeypatch.setattr(tui, "project_overview", lambda config: [pinned, project("beta"), project("gamma")])
    stub_tmux(monkeypatch)
    app = tui.GopherTUI(config, pinned="alpha")

    async def steps(app, pilot):
        app.project_list().focus()
        await pilot.press("j")
        assert app.selected()["name"] == "gamma"
        app.refocus_when_idle()
        assert app.focused is app.project_list()
        app.last_input -= app.idle_after + 1
        await pilot.press("slash")
        app.last_input -= app.idle_after + 1
        app.refocus_when_idle()
        assert app.focused is app.search_box()
        await pilot.press("escape")
        await pilot.pause()
        app.last_input -= app.idle_after + 1
        app.refocus_when_idle()
        await pilot.pause()
        assert isinstance(app.focused, tui.Button) and str(app.focused.label) == "b Inbox"
        assert app.selected()["name"] == "beta"

    drive_app(app, steps)


def test_projects_without_an_open_window_get_a_gray_bar():
    rows = tui.mark_open_windows([project("alpha"), project("beta")], {"alpha", "gopher"})
    assert [tui.state_class(row) for row in rows] == ["pstate-active", "pstate-other"]
    assert tui.state_class(tui.mark_open_windows([project("beta")], None)[0]) == "pstate-active"


def test_backup_line_sits_below_the_footer(config, monkeypatch):
    monkeypatch.setattr(tui, "project_overview", lambda config: [project("alpha")])
    monkeypatch.setattr(tui, "backup_status", lambda root: tui.BackupStatus(last_commit=time.time() - 600, changed=2, added=12, removed=3, unpushed=0))
    stub_tmux(monkeypatch)
    app = tui.GopherTUI(config)

    async def steps(app, pilot):
        line = app.query_one("#backup", tui.Static)
        rendered = line.render()
        assert str(rendered) == "\U0001f4be 10m ago (+12 -3 ~2)  \U0001f552 off"
        styles = {str(rendered)[span.start:span.end]: str(span.style) for span in rendered.spans}
        assert (styles["+12"], styles["-3"], styles["~2"]) == ("ansi_green", "ansi_red", "ansi_yellow")
        assert line.region.y > app.query_one(tui.Footer).region.y

    drive_app(app, steps)


def test_health_lines_show_gopher_state_and_doctor_problems(config, monkeypatch):
    monkeypatch.setattr(tui, "project_overview", lambda config: [project("alpha")])
    monkeypatch.setattr(tui, "backup_status", lambda root: tui.BackupStatus(last_commit=time.time() - 600, unpushed=0))
    monkeypatch.setattr(tui, "gopher_state", lambda config: tui.CREDS)
    problems = [tui.doctor.Check(tui.doctor.WARN, "schedule", "meeting-notes failed (exit 1): login expired")]
    monkeypatch.setattr(tui.doctor, "run", lambda config, now=None: list(problems))
    stub_tmux(monkeypatch)
    app = tui.GopherTUI(config)

    async def steps(app, pilot):
        health = app.query_one("#health", tui.Static)
        assert str(health.render()).splitlines() == [
            "gopher \u2715 needs login", "\u26a0 schedule: meeting-notes failed (exit 1): login expired",
        ]
        assert health.region.y > app.query_one("#backup", tui.Static).region.y
        await pilot.press("D")
        assert str(health.render()) == "gopher \u2715 needs login"
        assert tui.doctor.undismissed(config, problems) == []
        problems[0] = tui.doctor.Check(tui.doctor.WARN, "schedule", "meeting-notes failed (exit 1): other")
        assert len(tui.doctor.undismissed(config, problems)) == 1

    drive_app(app, steps)


def test_health_text_caps_the_problem_lines():
    checks = [tui.doctor.Check(tui.doctor.FAIL, "hooks", f"problem {n}") for n in range(5)]
    lines = str(tui.health_text(tui.IDLE, checks, 80)).splitlines()
    assert lines[0] == "gopher \u25cb idle" and len(lines) == 4
    assert lines[-1] == "+3 more: gopher doctor (D dismisses)"
    assert str(tui.health_text(None, [], 80)) == "gopher not running"


def test_open_todos_feed_the_pinned_header(tmp_path):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "todo.md").write_text("# To-do\n\n- [ ] first\n- [x] done\n  * [ ] nested\nnot a task\n- [ ]\n")
    assert overview.open_todos(tmp_path) == ["first", "nested"]
    assert overview.open_todos(tmp_path / "missing") == []
    row = {"todos": [f"t{i}" for i in range(7)]}
    text = tui.todos_text(row).plain
    assert text.startswith("\nto-do (7)") and "t4" in text and "t5" not in text and "+2 more" in text
    assert "t6" in tui.todos_text(row, limit=None).plain
    assert tui.todos_text({"todos": []}).plain == ""


def test_focused_panes_skip_clients_with_a_gopher_popup_open(monkeypatch):
    listing = "/dev/pts/1\t%1\tdefault\n/dev/pts/2\t%2\t" + tui.popup.DIM_STYLE + "\n/dev/pts/3\t%3\n"
    monkeypatch.setattr(tui, "tmux", lambda *argv: subprocess.CompletedProcess(argv, 0, listing, ""))
    assert tui.focused_panes() == {"/dev/pts/1": "%1", "/dev/pts/3": "%3"}
