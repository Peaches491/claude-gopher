"""Record docs/images/demo.gif: gopher sidebars beside agent panes, in a throwaway tmux server.

Usage: uv run python scripts/demo_tmux.py OUT.gif [--size 150x42] [--max-width 1400]

Builds the made-up root from scripts/demo.py in a temporary home, starts a private tmux server
(socket gopher-demo) with one window per project that has agents, runs the real sidebar in each
window beside a scripted stand-in for the agent, then drives the sidebars with send-keys and
captures every pane into GIF frames. Needs tmux, a headless-capable Chrome (google-chrome or
$CHROME) and ImageMagick (convert). Nothing touches your own tmux server, root, or Claude Code.

The sidebar and agent subcommands are what the demo panes run.
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from rich.console import Console
from rich.style import Style
from rich.text import Text

import demo
from claude_gopher import state
from claude_gopher.config import load_config

SOCKET = "gopher-demo"
REAL_HOME = os.environ.get("HOME", "")
SESSION = "demo"
SIDEBAR_WIDTH = 50
SESSIONS_FILE = "demo-sessions.json"
CAPTURE_INTERVAL = 0.5
GLYPHS = {"working": "\u2605", "waiting": "\u25cf", "idle": "\u25cb", "creds": "\u2715"}
SPINNER = "\u2722\u2733\u2736\u273b\u273d\u273b\u2736\u2733"
BULLET, RESULT, RULE = "\u25cf", "\u23bf", "\u2500"

BORDER = Style(color="color(240)")
ACTIVE_BORDER = Style(color="green")
STATUS = Style(color="black", bgcolor="green")
STATUS_CURRENT = Style(color="black", bgcolor="green", bold=True, reverse=True)

GOPHER = "gopher"
TRANSCRIPTS = {
    GOPHER: {
        "prompt": "Morning. What needs me today?",
        "said": [
            "One decision and one blocker before standup:",
            "- payments-retry is waiting on you: which backoff schedule the retry queue uses.",
            "- flaky-ci is blocked on expired cloud credentials; log in and tell its agent to retry.",
            "Two PRs are in review (#1482, #1479) and docs-refresh's guide PR #1475 needs a reviewer.",
            "I saved the root at 08:00: 4 commits, pushed.",
        ],
    },
    "api-v2-migration": {
        "prompt": "Port the order endpoints to v2 behind the api_v2 flag",
        "tools": [("Edit", "src/api/v2/orders.py", "Added 84 lines, removed 12"), ("Bash", "pytest tests/v2 -q", "48 passed in 6.2s")],
        "said": ["GET and POST /orders now route through v2 when the flag is on. Running the contract tests against the client SDK next."],
        "working": "Running contract tests",
    },
    "payments-retry": {
        "prompt": "Design the retry queue for failed captures",
        "tools": [("Read", "docs/payments/retries.md", "Read 120 lines")],
        "said": ["The queue design is agreed in the doc. One choice is yours before I build it:"],
        "question": (
            "Which backoff schedule should the retry queue use?",
            ["Exponential, 2s doubling to 5m (Recommended)", "Fixed, every 30s", "Linear, +30s per attempt"],
        ),
    },
    "search-ranking": {
        "prompt": "Compare BM25 and hybrid ranking on last month's queries",
        "tools": [("Bash", "python eval/run.py --queries 2026-10 --rankers bm25,hybrid", "Running in the background")],
        "said": ["The harness is through about two thirds of the queries. Hybrid leads on nDCG@10 so far (0.412 vs 0.371)."],
        "working": "Evaluating 41,200 / 60,000 queries",
    },
    "docs-refresh": {
        "prompt": "Rewrite the getting-started guide for the new CLI",
        "tools": [("Edit", "docs/getting-started.md", "Added 140 lines, removed 96"), ("Bash", "git push -u origin docs-refresh", "Pushed")],
        "said": ["The guide is rewritten and PR #1475 is up for review. Nothing else to do until it gets comments."],
    },
    "api-v2-migration#1": {
        "prompt": "Draft release notes for the v2 client SDK",
        "said": ["Drafted docs/sdk/CHANGELOG.md from the merged PRs. Ready for your edits."],
    },
    "flaky-ci": {
        "prompt": "Find out why the integration job flakes",
        "tools": [("Bash", "ci logs integration --last 20", "6 of 20 failed, all in upload_artifacts")],
        "said": [
            "Two jobs share one temp dir, so one deletes the other's artifacts. The fix is in, but the upload step "
            "now fails: the cloud credentials have expired. Log in again and tell me to retry.",
        ],
    },
    "onboarding-flow": {
        "prompt": "Implement the first onboarding screen from the approved wireframes",
        "tools": [("Edit", "web/onboarding/Welcome.tsx", "Added 210 lines"), ("Bash", "npm test -- onboarding", "12 passed")],
        "said": ["The welcome screen is in, with analytics events stubbed. Waiting on the new copy from design."],
    },
}
DETAILS = {
    "payments-retry": "Retry queue for failed card captures. Design agreed in docs/payments/retries.md.\n"
                      "Waiting on you: pick the backoff schedule. PR #1490 has the queue skeleton.",
    "api-v2-migration": "Porting public endpoints to v2 behind a flag. Orders done, PRs #1482 and #1479\n"
                        "in review; the client SDK update is next.",
}


def tmux(*argv: str, check: bool = True) -> str:
    return subprocess.run(["tmux", "-L", SOCKET, *argv], check=check, capture_output=True, text=True).stdout


def sidebar_main(root: Path, project: str) -> int:
    config = load_config(root)
    sessions_file = config.state_dir / SESSIONS_FILE

    def current_sessions(config, prune=True):
        locations = state.pane_locations() or {}
        sessions = json.loads(sessions_file.read_text())
        return [{**s, "location": locations.get(s.get("pane") or "")} for s in sessions]

    demo.overview.current_sessions = current_sessions
    demo.tui.summarize = lambda *args, **kwargs: "(summary)"
    app = demo.make_app(config, [], None if project == "-" else project)
    app.run()
    return 0


def agent_main(project: str, state: str) -> int:
    script = TRANSCRIPTS[project]
    width, height = shutil.get_terminal_size()
    wrap = max(width - 4, 20)
    lines = ["", f"\x1b[48;5;236m\x1b[37m > {script['prompt']}".ljust(width + 12) + "\x1b[0m", ""]
    for tool, argument, result in script.get("tools", []):
        lines.append(f"\x1b[32m{BULLET}\x1b[0m \x1b[1m{tool}\x1b[0m({argument})")
        lines.append(f"  \x1b[2m{RESULT}  {result}\x1b[0m")
        lines.append("")
    for paragraph in script.get("said", []):
        for number, line in enumerate(textwrap.wrap(paragraph, wrap)):
            lines.append(("\x1b[37m" + BULLET + "\x1b[0m " if number == 0 and not paragraph.startswith("-") else "  ") + line)
    if question := script.get("question"):
        text, options = question
        lines += ["", "\x1b[34m" + RULE * (width - 2) + "\x1b[0m", f" \x1b[1;34m{text}\x1b[0m", ""]
        lines += [f" {'>' if i == 0 else ' '} {i + 1}. {option}" for i, option in enumerate(options)]
        lines += ["", " \x1b[2mEnter to select - Esc to cancel\x1b[0m"]
    sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(lines[: height - 6]))
    box = height - 3
    sys.stdout.write(f"\x1b[{box};1H\x1b[2m{RULE * width}\x1b[0m\x1b[{box + 1};1H > \x1b[{box + 2};1H\x1b[2m{RULE * width}\x1b[0m")
    sys.stdout.flush()
    started = time.time()
    tick = 0
    while True:
        if script.get("working") and state == "working":
            elapsed = int(time.time() - started) + 37
            sys.stdout.write(
                f"\x1b[{box - 1};1H\x1b[2K\x1b[38;5;173m{SPINNER[tick % len(SPINNER)]} {script['working']}...\x1b[0m "
                f"\x1b[2m({elapsed}s - esc to interrupt)\x1b[0m\x1b[{box + 1};4H"
            )
            sys.stdout.flush()
        tick += 1
        time.sleep(0.25)


def pane_rows(window: str) -> list[dict]:
    rows = []
    listing = tmux("list-panes", "-t", window, "-F", "#{pane_id} #{pane_left} #{pane_top} #{pane_width} #{pane_height} #{pane_active}")
    for line in listing.splitlines():
        pane, left, top, width, height, active = line.split()
        lines = tmux("capture-pane", "-e", "-p", "-t", pane).split("\n")
        rows.append({
            "left": int(left), "top": int(top), "width": int(width), "height": int(height),
            "active": active == "1", "lines": [Text.from_ansi(text) for text in lines],
        })
    return rows


def status_line(current: str, width: int) -> Text:
    text = Text(f"[{SESSION}] ", style=STATUS)
    for line in tmux("list-windows", "-t", SESSION, "-F", "#{window_index}\t#{window_name}\t#{window_id}").splitlines():
        index, name, window = line.split("\t")
        text.append(f"{index}:{name}", style=STATUS_CURRENT if window == current else STATUS)
        text.append(" ", style=STATUS)
    text.truncate(width, pad=True)
    text.stylize(STATUS)
    return text


def capture(width: int, height: int) -> list[Text]:
    window = current_window()
    layout = pane_rows(window)
    rows = []
    for y in range(height - 1):
        covering = sorted((p for p in layout if p["top"] <= y < p["top"] + p["height"]), key=lambda p: p["left"])
        if not covering:
            above = next((p for p in layout if p["top"] + p["height"] == y), None)
            rows.append(Text(RULE * width, style=ACTIVE_BORDER if above and above["active"] else BORDER))
            continue
        row, x = Text(), 0
        for pane in covering:
            if pane["left"] > x:
                active = pane["active"] or any(p["active"] and p["left"] + p["width"] == pane["left"] - 1 for p in covering)
                row.append("\u2502" * (pane["left"] - x), style=ACTIVE_BORDER if active else BORDER)
            offset = y - pane["top"]
            line = pane["lines"][offset].copy() if offset < len(pane["lines"]) else Text()
            line.truncate(pane["width"], pad=True)
            row.append_text(line)
            x = pane["left"] + pane["width"]
        if x < width:
            row.append(" " * (width - x))
        rows.append(row)
    rows.append(status_line(window, width))
    return rows


def current_window() -> str:
    return tmux("display-message", "-p", "-t", f"={SESSION}:", "#{window_id}").strip()


def to_svg(rows: list[Text], width: int) -> str:
    console = Console(width=width, record=True, file=open(os.devnull, "w"), color_system="truecolor", legacy_windows=False)
    for row in rows:
        console.print(row, no_wrap=True, overflow="crop")
    return demo.clean_svg(console.export_svg(title="gopher"))


def to_png(svg: Path, png: Path) -> None:
    text = svg.read_text()
    box = text[text.find('viewBox="') + 9:].split('"')[0].split()
    subprocess.run(
        [os.environ.get("CHROME", "google-chrome"), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--force-device-scale-factor=1", f"--window-size={int(float(box[2]))},{int(float(box[3]))}",
         f"--screenshot={png}", f"file://{svg}"],
        check=True, capture_output=True, env={**os.environ, "HOME": REAL_HOME},
    )


class Recorder:
    def __init__(self, width: int, height: int, sidebars: dict[str, str]):
        self.width, self.height, self.sidebars = width, height, sidebars
        self.frames: list[tuple[list[Text], float]] = []

    def wait(self, seconds: float) -> None:
        end = time.time() + seconds
        while True:
            left = end - time.time()
            self.frames.append((capture(self.width, self.height), min(CAPTURE_INTERVAL, max(left, 0.1))))
            if left <= CAPTURE_INTERVAL:
                return
            time.sleep(CAPTURE_INTERVAL)

    def send(self, key: str, literal: bool = False) -> None:
        tmux("send-keys", "-t", self.sidebars[current_window()], *(["-l"] if literal else []), "--", key)

    def key(self, key: str, hold: float = 0.6, settle: float = 0.4, literal: bool = False) -> None:
        self.send(key, literal)
        time.sleep(settle)
        self.wait(hold)

    def type(self, text: str, chunk: int = 3) -> None:
        for start in range(0, len(text), chunk):
            self.send(text[start:start + chunk], literal=True)
            time.sleep(0.12)
            self.frames.append((capture(self.width, self.height), 0.12))

    def search(self, text: str) -> None:
        self.key("/", hold=0.4, literal=True)
        self.type(text)
        self.key("Enter", hold=0.8)

    def jump(self, hold: float) -> None:
        self.key("t", hold=hold, settle=1.2)

    def select(self, window: str, hold: float) -> None:
        tmux("select-window", "-t", window)
        time.sleep(1.0)
        self.wait(hold)


def storyboard(rec: Recorder, windows: dict[str, str]) -> None:
    rec.select(windows["api-v2-migration"], 3.0)
    for key in "jjj":
        rec.key(key)
    rec.key("k", hold=1.0)
    rec.search("pay")
    rec.jump(4.0)
    rec.search("flaky")
    rec.key("m", hold=0.8)
    rec.type("Credentials refreshed; retry the upload", chunk=4)
    rec.wait(0.6)
    rec.key("Enter", hold=2.0, settle=0.6)
    rec.jump(2.5)
    rec.key("b", hold=3.0)
    rec.key("Escape", hold=0.6)
    rec.select(windows[GOPHER], 3.0)
    for key in "jjj":
        rec.key(key)
    rec.key("k", hold=3.0)


def window_name(project: str, sessions: list[dict]) -> str:
    states = [s["state"] for s in sessions if s["project"] == project]
    if any(s.get("creds") for s in sessions if s["project"] == project):
        glyph = GLYPHS["creds"]
    else:
        glyph = next((GLYPHS[s] for s in ("waiting", "working") if s in states), GLYPHS["idle"])
    return f"{glyph} {project}"


def build_server(home: Path, config, sessions: list[dict], width: int, height: int) -> tuple[dict, dict]:
    conf = home / "tmux.conf"
    conf.write_text(
        "set -g status off\nset -g window-size manual\n"
        f"set -g default-size {width}x{height - 1}\nset -g default-terminal tmux-256color\n"
        "set -g pane-border-lines single\nset -g escape-time 10\n"
    )
    env = {**os.environ, **demo.git_env(home), "GOPHER_HOME": str(config.root), "COLORTERM": "truecolor"}
    for key in ("TMUX", "TMUX_PANE", "GOPHER_PROJECT"):
        env.pop(key, None)
    script = shlex.quote(str(Path(__file__).resolve()))
    python = shlex.quote(sys.executable)
    root = shlex.quote(str(config.root))

    def sidebar(project: str) -> str:
        return f"{python} {script} sidebar {root} {project}"

    def agent(project: str, state: str) -> str:
        return f"{python} {script} agent {project} {state}"

    subprocess.run(
        ["tmux", "-L", SOCKET, "-f", str(conf), "new-session", "-d", "-s", SESSION, "-n", f"{GLYPHS['idle']} {GOPHER}",
         "-x", str(width), "-y", str(height - 1), "-c", str(config.root), sidebar("-")],
        check=True, env=env,
    )
    windows, sidebars = {}, {}
    first = tmux("display-message", "-p", "-t", f"={SESSION}:", "#{window_id} #{pane_id}").split()
    windows[GOPHER], sidebars[first[0]] = first[0], first[1]
    tmux("split-window", "-h", "-d", "-t", first[1], "-l", str(width - SIDEBAR_WIDTH - 1), agent(GOPHER, "idle"))
    for spec in demo.PROJECTS:
        project = spec["name"]
        mine = [s for s in sessions if s["project"] == project]
        if not mine:
            continue
        window, pane = tmux(
            "new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}", "-t", f"={SESSION}:",
            "-n", window_name(project, sessions), sidebar(project),
        ).split()
        windows[project], sidebars[window] = window, pane
        target = pane
        for number, session in enumerate(mine):
            flags = ["-h", "-l", str(width - SIDEBAR_WIDTH - 1)] if number == 0 else ["-v", "-l", str((height - 1) // 3)]
            session["pane"] = tmux(
                "split-window", *flags, "-d", "-P", "-F", "#{pane_id}", "-t", target,
                agent(project if number == 0 else f"{project}#{number}", session["state"]),
            ).strip()
            target = session["pane"]
        tmux("select-pane", "-t", pane)
    for session in sessions:
        session.pop("creds", None)
    (config.state_dir / SESSIONS_FILE).write_text(json.dumps(sessions))
    return windows, sidebars


def demo_sessions(sessions: list[dict]) -> list[dict]:
    creds = {spec["name"] for spec in demo.PROJECTS for agent in spec.get("agents", []) if len(agent) > 2}
    return [{**{k: v for k, v in s.items() if k != "location"}, "creds": s["project"] in creds} for s in sessions]


def record(out: Path, width: int, height: int, max_width: int) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        os.environ.update(demo.git_env(home))
        os.environ["GOPHER_HOME"] = str(home / "workon")
        config, sessions = demo.build_root(home)
        for spec in demo.PROJECTS:
            if spec["name"] in DETAILS:
                demo.tui.SummaryCache(config, "details").put(spec["name"], DETAILS[spec["name"]])
            if spec.get("haiku"):
                demo.tui.SummaryCache(config).put(spec["name"], spec["haiku"])
        demo.back_up_root(home, config.root)
        sessions = demo_sessions(sessions)
        windows, sidebars = build_server(home, config, sessions, width, height)
        client = subprocess.Popen(["tmux", "-L", SOCKET, "-C", "attach", "-t", SESSION], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        try:
            time.sleep(6.0)
            rec = Recorder(width, height, sidebars)
            storyboard(rec, windows)
        finally:
            client.stdin.close()
            client.wait(timeout=10)
            tmux("kill-server", check=False)
        frames = home / "frames"
        frames.mkdir()
        command, seen = ["convert", "-loop", "0"], {}
        for number, (rows, hold) in enumerate(rec.frames):
            svg = to_svg(rows, width)
            digest = hashlib.md5(svg.encode()).hexdigest()
            if digest not in seen:
                path = frames / f"{number:04d}.svg"
                path.write_text(svg)
                seen[digest] = frames / f"{number:04d}.png"
                to_png(path, seen[digest])
            command += ["-delay", str(max(int(hold * 100), 2)), str(seen[digest])]
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([*command, "-resize", f"{max_width}x>", "-layers", "Optimize", str(out)], check=True)
        print(f"{out}: {len(rec.frames)} frames, {len(seen)} distinct")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "sidebar":
        return sidebar_main(Path(sys.argv[2]), sys.argv[3])
    if len(sys.argv) > 1 and sys.argv[1] == "agent":
        return agent_main(sys.argv[2], sys.argv[3])
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--size", default="150x42")
    parser.add_argument("--max-width", type=int, default=1400)
    args = parser.parse_args()
    width, height = (int(part) for part in args.size.split("x"))
    if tmux("list-sessions", check=False).strip():
        raise SystemExit(f"a tmux server is already running on socket {SOCKET}; kill it with: tmux -L {SOCKET} kill-server")
    record(args.out.resolve(), width, height, args.max_width)
    return 0


if __name__ == "__main__":
    sys.exit(main())
