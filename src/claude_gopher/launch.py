import datetime
import fcntl
import shlex
import subprocess

from claude_gopher import schedule
from claude_gopher.config import Config, gopher_features
from claude_gopher.prompt import SESSION_CONTEXTS, assemble
from claude_gopher.workon import SIDEBAR_COMMANDS, Agent, agent_argv, pick_agent, run_lines

SESSION = "gopher"
WINDOW = "gopher"
SCHEDULE_WINDOW = "schedule"
LINK_INDEX = "0"
SCHEDULE_RETRY_SECONDS = 30
DEFAULT_MODEL = "sonnet"
PROMPT_FILE = "gopher-prompt.md"
PROMPT_ENV = "GOPHER_PROMPT_FILE"
GOPHER_KICKOFF = (
    "Start of session: read every project's STATUS.md frontmatter and human/STATUS.md, and today's journal, "
    "then give me a short briefing and ask what I'd like to do."
)
GOPHER_RESTART_KICKOFF = (
    "You were just restarted (for example after a gopher update), continuing this conversation. The schedule runs "
    "in the background now, so there is nothing to reschedule; if a heartbeat CronCreate job is still listed, delete "
    "it. Then tell me in one line that you are back."
)
AGENT_STATUS = """---
summary: Gopher agent directory.
state: active
updated: {date}
---
# Gopher
"""


def tmux(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *argv], check=check, capture_output=True, text=True)


def out(*argv: str) -> str:
    return tmux(*argv).stdout.strip()


def prompt_path(config: Config):
    return config.state_dir / PROMPT_FILE


def write_prompt(config: Config):
    path = prompt_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(assemble(config, context) for context in SESSION_CONTEXTS["gopher"]))
    return path


def gopher_agent(config: Config) -> Agent:
    settings = config.settings.get("gopher", {})
    agent = pick_agent(config, settings.get("agent"))
    if agent.name != "claude":
        return agent
    command = list(agent.command)
    model = settings.get("model", DEFAULT_MODEL)
    if model and "--model" not in command:
        command += ["--model", model]
    if "--name" not in command:
        command += ["--name", SESSION]
    if "--permission-mode" not in command:
        command += ["--permission-mode", "auto"]
    if "--append-system-prompt-file" not in command:
        command += ["--append-system-prompt-file", str(prompt_path(config))]
    return Agent(agent.name, command, agent.resume_flags, agent.kickoff)


def ensure_agent_dir(config: Config) -> None:
    for sub in ("journal", "pending", *(["notebook"] if "notebook" in gopher_features(config) else [])):
        (config.agent_dir / sub).mkdir(parents=True, exist_ok=True)
    status = config.agent_dir / "STATUS.md"
    if not status.exists():
        status.write_text(AGENT_STATUS.format(date=datetime.date.today().isoformat()))


def agent_lines(config: Config, passthrough: list[str], resume: bool = False) -> list[str]:
    agent = gopher_agent(config)
    if resume and agent.name == "claude":
        argv = [*agent.command, "--continue", *passthrough, GOPHER_RESTART_KICKOFF]
    else:
        argv = agent_argv(agent, passthrough, GOPHER_KICKOFF)
    return [
        f"cd {shlex.quote(str(config.agent_dir))}",
        f"export GOPHER_HOME={shlex.quote(str(config.root))} GOPHER_AGENT=gopher"
        + (f" {PROMPT_ENV}={shlex.quote(str(prompt_path(config)))}" if gopher_agent(config).name == "claude" else ""),
        *config.settings.get("workon", {}).get("pre_launch", []),
        shlex.join(argv),
    ]


def exec_agent(config: Config, passthrough: list[str], resume: bool = False) -> None:
    ensure_agent_dir(config)
    write_prompt(config)
    run_lines(agent_lines(config, passthrough, resume))


def agent_command(config: Config, resume: bool = False) -> str:
    return f"gopher --root {shlex.quote(str(config.root))} launch --exec-agent" + (" --continue" if resume else "")


def has_session(name: str) -> bool:
    return tmux("has-session", "-t", f"={name}", check=False).returncode == 0


def gopher_window() -> str:
    return out("list-windows", "-t", f"={SESSION}", "-F", "#{window_id}").splitlines()[0]


def create_session(config: Config) -> str:
    directory = str(config.agent_dir)
    agent = out("new-session", "-d", "-s", SESSION, "-n", WINDOW, "-c", directory, "-P", "-F", "#{pane_id}")
    tmux("send-keys", "-t", agent, agent_command(config), "Enter")
    return gopher_window()


def schedule_command(config: Config) -> str:
    loop = f"gopher --root {shlex.quote(str(config.root))} schedule run"
    retry = f'echo "schedule loop exited ($?); restarting in {SCHEDULE_RETRY_SECONDS}s"; sleep {SCHEDULE_RETRY_SECONDS}'
    return "sh -c " + shlex.quote(f"while :; do {loop}; {retry}; done")


def schedule_pane() -> str | None:
    listing = tmux("list-panes", "-s", "-t", f"={SESSION}:", "-F", "#{window_name}\t#{pane_id}", check=False).stdout
    return next((pane for name, _, pane in (line.partition("\t") for line in listing.splitlines()) if name == SCHEDULE_WINDOW), None)


def ensure_schedule(config: Config, respawn: bool = False) -> str:
    pane = schedule_pane()
    if pane and not respawn:
        return "schedule loop running"
    if pane:
        tmux("respawn-pane", "-k", "-t", pane, "-c", str(config.root))
        message = "restarted the schedule loop"
    else:
        pane = out("new-window", "-d", "-t", f"={SESSION}:", "-n", SCHEDULE_WINDOW, "-c", str(config.root), "-P", "-F", "#{pane_id}")
        message = "started the schedule loop"
    tmux("send-keys", "-t", pane, schedule_command(config), "Enter")
    return message


def heal_schedule(config: Config) -> str | None:
    if not has_session(SESSION) or schedule.loop_running(config):
        return None
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with open(config.state_dir / "schedule-heal.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        if schedule.loop_running(config):
            return None
        return ensure_schedule(config, respawn=schedule_pane() is not None)


def agent_pane(window: str) -> str:
    panes = [line.split("\t") for line in out("list-panes", "-t", window, "-F", "#{pane_id}\t#{pane_current_command}").splitlines()]
    return next((pane for pane, command in panes if command not in SIDEBAR_COMMANDS), panes[0][0])


def restart(config: Config, resume: bool) -> list[str]:
    if not has_session(SESSION):
        return launch(config, None)
    ensure_agent_dir(config)
    pane = agent_pane(gopher_window())
    tmux("respawn-pane", "-k", "-t", pane, "-c", str(config.agent_dir))
    tmux("send-keys", "-t", pane, agent_command(config, resume), "Enter")
    return [
        f"restarted Gopher in {pane}" + (" (continuing its last conversation)" if resume else " (fresh session)"),
        ensure_schedule(config, respawn=True),
    ]


def current_session() -> str | None:
    result = tmux("display-message", "-p", "#{session_name}", check=False)
    name = result.stdout.strip()
    return name if result.returncode == 0 and name else None


def window_at(session: str, index: str) -> str | None:
    for line in out("list-windows", "-t", f"={session}", "-F", "#{window_index} #{window_id}").splitlines():
        found, window = line.split()
        if found == index:
            return window
    return None


def linked_index(window: str, session: str) -> str | None:
    for line in out("list-windows", "-t", f"={session}", "-F", "#{window_index} #{window_id}").splitlines():
        index, found = line.split()
        if found == window:
            return index
    return None


def relink(session: str) -> str | None:
    if not has_session(SESSION):
        return None
    window = gopher_window()
    index = linked_index(window, session)
    if index is None or index == LINK_INDEX or window_at(session, LINK_INDEX):
        return None
    tmux("move-window", "-s", f"={session}:{index}", "-t", f"={session}:{LINK_INDEX}")
    return f"moved back to {session}:{LINK_INDEX} from {index}"


def link(window: str, session: str) -> str:
    if moved := relink(session):
        return moved
    existing = window_at(session, LINK_INDEX)
    if existing == window:
        return f"already linked at {session}:{LINK_INDEX}"
    moved = ""
    if existing:
        name = out("display-message", "-p", "-t", existing, "#{window_name}")
        used = {int(line.split()[0]) for line in out("list-windows", "-t", f"={session}", "-F", "#{window_index} #{window_id}").splitlines()}
        free = next(index for index in range(1, len(used) + 2) if index not in used)
        tmux("move-window", "-s", f"={session}:{LINK_INDEX}", "-t", f"={session}:{free}")
        moved = f"moved window {name!r} from {session}:{LINK_INDEX} to {session}:{free}; "
    tmux("link-window", "-d", "-s", window, "-t", f"={session}:{LINK_INDEX}")
    return f"{moved}linked into {session}:{LINK_INDEX}"


def launch(config: Config, session: str | None) -> list[str]:
    ensure_agent_dir(config)
    report = []
    if has_session(SESSION):
        window = gopher_window()
        report.append(f"gopher session exists ({window})")
    else:
        window = create_session(config)
        report.append(f"started gopher session ({window})")
    report.append(ensure_schedule(config))
    target = session or current_session()
    if target and target != SESSION:
        report.append(link(window, target))
    return report
