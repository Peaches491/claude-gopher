"""Build a throwaway gopher root with made-up projects and capture the TUI as SVG frames.

Usage: uv run python scripts/demo.py OUT_DIR

Writes OUT_DIR/dashboard.svg, OUT_DIR/sidebar.svg and OUT_DIR/frames/NNN.svg (a scripted walk
through the sidebar). Nothing here touches tmux, Claude Code, or the real root.
"""

import asyncio
import datetime
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from claude_gopher import overview, tui
from claude_gopher.checkouts import add_checkout
from claude_gopher.config import load_config
from claude_gopher.roots import create_project, create_root

TODAY = datetime.date.today()
NOW = time.time()

PROJECTS = [
    {
        "name": "api-v2-migration", "state": "active", "days": 0, "priority": 2,
        "summary": "Endpoints ported to v2 behind a flag; two PRs in review, client SDK next",
        "haiku": "v2 endpoints behind a flag; SDK update next",
        "prs": [("1482", "open"), ("1479", "open")], "tickets": ["API-212"],
        "checkouts": ["api-server", "api-server-v2", "client-sdk"],
        "agents": [("working", None), ("idle", None)], "names": ["port the order endpoints", "SDK release notes"],
    },
    {
        "name": "payments-retry", "state": "active", "days": 0, "priority": 3,
        "summary": "Retry queue design agreed; agent is asking which backoff schedule to use",
        "haiku": "Needs your call on the backoff schedule",
        "prs": [("1490", "open")], "tickets": ["PAY-88"], "inbox": [("Ship before the freeze on Friday", True)],
        "checkouts": ["payments"], "agents": [("waiting", None)],
    },
    {
        "name": "search-ranking", "state": "active", "days": 1, "priority": 1,
        "summary": "Offline eval harness running on last month's queries; comparing BM25 and hybrid",
        "haiku": "Eval harness running; hybrid ahead so far",
        "checkouts": ["search"], "agents": [("working", None)],
    },
    {
        "name": "docs-refresh", "state": "waiting-review", "days": 0,
        "summary": "Getting-started guide rewritten; PR up for review",
        "haiku": "Guide rewritten, PR waiting on review",
        "prs": [("1475", "open")], "checkouts": ["docs-site"], "agents": [("idle", NOW - 600)],
    },
    {
        "name": "flaky-ci", "state": "blocked", "days": 2,
        "summary": "Narrowed the flake to a shared temp dir; blocked on expired cloud credentials",
        "haiku": "Blocked: cloud credentials expired",
        "tickets": ["CI-301"], "checkouts": ["build-infra"], "agents": [("idle", None, True)],
    },
    {
        "name": "onboarding-flow", "state": "active", "days": 3,
        "summary": "Wireframes approved; first screen implemented, analytics events stubbed",
        "inbox": [("Add the new copy from design", False), ("Check the empty-state illustration", False)],
        "checkouts": ["web-app"], "agents": [("idle", None)],
    },
    {
        "name": "perf-dashboard", "state": "waiting-review", "days": 4,
        "summary": "p95 latency panels drafted; waiting on the team's review of the layout",
        "prs": [("1460", "merged")],
    },
    {
        "name": "cache-layer", "state": "active", "days": 6,
        "summary": "Read-through cache prototype; measuring hit rate before choosing an eviction policy",
        "checkouts": ["api-server"],
    },
]

PINNED = "api-v2-migration"
CONFIG_EXTRA = """
[[tui.actions]]
key = "s"
label = "Git"
command = "git status --short --branch"
description = "Show the checkout's branch and uncommitted changes"
"""


def git_env(home: Path) -> dict:
    return {
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
        "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com",
    }


def write_resource(directory: Path, name: str, url: str, state: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    merged = f"merged: {TODAY.isoformat()}\n" if state == "merged" else ""
    (directory / f"{name}.md").write_text(f"---\nstate: {state}\n{merged}---\n# [{name}]({url})\n")


def build_root(home: Path):
    root = home / "workon"
    create_root(root)
    (root / "config.toml").write_text((root / "config.toml").read_text() + CONFIG_EXTRA)
    config = load_config(root)
    sessions = []
    for index, spec in enumerate(PROJECTS):
        name = spec["name"]
        project_dir = create_project(config.projects_dir, name)
        updated = (TODAY - datetime.timedelta(days=spec["days"])).isoformat()
        (project_dir / "STATUS.md").write_text(
            f"---\nsummary: {spec['summary']}\nstate: {spec['state']}\nupdated: {updated}\n---\n# {name}\n"
        )
        if spec.get("priority"):
            human = project_dir / "human"
            human.mkdir(exist_ok=True)
            (human / "STATUS.md").write_text(f"---\npriority: {spec['priority']}\n---\n")
        for number, state in spec.get("prs", []):
            write_resource(project_dir / "resources" / "prs", number, f"https://example.com/pull/{number}", state)
        for key in spec.get("tickets", []):
            write_resource(project_dir / "resources" / "tickets", key, f"https://example.com/browse/{key}", "open")
        for item, (text, wake) in enumerate(spec.get("inbox", [])):
            inbox = project_dir / "inbox"
            inbox.mkdir(exist_ok=True)
            wake_line = "wake: true\n" if wake else ""
            (inbox / f"20261117-09000{item}-note.md").write_text(f"---\nfrom: user\n{wake_line}---\n{text}\n")
        for checkout in spec.get("checkouts", []):
            path = home / "src" / checkout
            if not path.exists():
                path.mkdir(parents=True)
                subprocess.run(["git", "init", "-q", str(path)], check=True)
            add_checkout(config.projects_dir, name, path)
        for agent, agent_spec in enumerate(spec.get("agents", [])):
            state, finished, *creds = agent_spec
            pane = f"%{index * 10 + agent}"
            sessions.append({
                "session_id": f"{name}-{agent}", "name": (spec.get("names") or [name])[agent % len(spec.get("names") or [name])], "project": name, "state": state, "pane": pane,
                "updated": NOW - agent * 60, "finished": finished, "cwd": str(home / "src"),
                "location": {
                    "session": "work", "window_index": str(index + 1), "pane_index": str(agent + 1),
                    "window_name": name, "blocked_creds": bool(creds),
                },
            })
    return config, sessions


def patch_live_sources(sessions: list[dict]) -> None:
    overview.current_sessions = lambda config, prune=True: [dict(s) for s in sessions]
    tui.focused_panes = lambda: {}
    tui.open_window_names = lambda: {s["project"] for s in sessions}


def clean_svg(svg: str) -> str:
    start = svg.find("@font-face")
    while start != -1:
        end = svg.find("}", start)
        svg = svg[:start] + svg[end + 1:]
        start = svg.find("@font-face")
    return svg.replace('"Fira Code"', '"DejaVu Sans Mono"')


class Recorder:
    def __init__(self, app, directory: Path):
        self.app, self.directory, self.count = app, directory, 0
        directory.mkdir(parents=True, exist_ok=True)

    def frame(self, repeat: int = 1) -> None:
        svg = clean_svg(self.app.export_screenshot(title="gopher"))
        for _ in range(repeat):
            (self.directory / f"{self.count:03d}.svg").write_text(svg)
            self.count += 1


def make_app(config, sessions, pinned):
    app = tui.GopherTUI(config, pinned=pinned)
    app.start_dev_reload = lambda: None
    for spec in PROJECTS:
        if spec.get("haiku"):
            app.cache.put(spec["name"], spec["haiku"])
    return app


async def settle(app, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def capture_dashboard(config, sessions, out: Path) -> None:
    app = make_app(config, sessions, None)
    async with app.run_test(size=(110, 42)) as pilot:
        await settle(app, pilot)
        (out / "dashboard.svg").write_text(clean_svg(app.export_screenshot(title="gopher tui")))


async def capture_sidebar(config, sessions, out: Path) -> None:
    app = make_app(config, sessions, PINNED)
    async with app.run_test(size=(52, 52)) as pilot:
        await settle(app, pilot)
        (out / "sidebar.svg").write_text(clean_svg(app.export_screenshot(title="gopher sidebar")))
        recorder = Recorder(app, out / "frames")
        recorder.frame(repeat=6)
        for key in ["j"] * 3 + ["k"] * 3:
            await pilot.press(key)
            await pilot.pause()
            recorder.frame(repeat=3)
        await pilot.press("k")
        await pilot.pause()
        recorder.frame(repeat=4)
        for key in ["l", "l", "l", "l", "l"]:
            await pilot.press(key)
            await pilot.pause()
            recorder.frame(repeat=6)
        await pilot.press("enter")
        await pilot.pause()
        recorder.frame(repeat=10)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        recorder.frame(repeat=3)
        await pilot.press("slash")
        await pilot.pause()
        for key in "pay":
            await pilot.press(key)
            await pilot.pause()
            recorder.frame(repeat=3)
        recorder.frame(repeat=6)
        await pilot.press("escape")
        await pilot.pause()
        recorder.frame(repeat=6)


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        os.environ.update(git_env(home))
        for key in ("TMUX", "TMUX_PANE", "GOPHER_PROJECT"):
            os.environ.pop(key, None)
        os.environ["GOPHER_HOME"] = str(home / "workon")
        config, sessions = build_root(home)
        patch_live_sources(sessions)
        asyncio.run(capture_dashboard(config, sessions, out))
        asyncio.run(capture_sidebar(config, sessions, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
