import datetime
import shlex
import subprocess

from claude_gopher.config import Config, gopher_features
from claude_gopher.prompt import SESSION_CONTEXTS, assemble
from claude_gopher.workon import Agent, WorkonError, agent_argv, pick_agent, run_lines

SESSION = "gopher"
WINDOW = "gopher"
LINK_INDEX = "0"
DEFAULT_MODEL = "sonnet"
PROMPT_FILE = "gopher-prompt.md"
PROMPT_ENV = "GOPHER_PROMPT_FILE"
GOPHER_KICKOFF = (
    "Start of session: read every project's STATUS.md frontmatter and human/STATUS.md, and today's journal, "
    "then give me a short briefing and ask what I'd like to do."
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


def agent_lines(config: Config, passthrough: list[str]) -> list[str]:
    argv = agent_argv(gopher_agent(config), passthrough, GOPHER_KICKOFF)
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
    if resume and gopher_agent(config).name == "claude":
        passthrough = ["--continue", *passthrough]
    run_lines(agent_lines(config, passthrough))


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


def restart(config: Config, resume: bool) -> list[str]:
    if not has_session(SESSION):
        return launch(config, None)
    ensure_agent_dir(config)
    pane = out("list-panes", "-t", gopher_window(), "-F", "#{pane_id}").splitlines()[0]
    tmux("respawn-pane", "-k", "-t", pane, "-c", str(config.agent_dir))
    tmux("send-keys", "-t", pane, agent_command(config, resume), "Enter")
    return [f"restarted Gopher in {pane}" + (" (continuing its last conversation)" if resume else " (fresh session)")]


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
    if existing:
        name = out("display-message", "-p", "-t", existing, "#{window_name}")
        raise WorkonError(f"{session}:{LINK_INDEX} is taken by window {name!r}; move it and rerun gopher launch")
    tmux("link-window", "-d", "-s", window, "-t", f"={session}:{LINK_INDEX}")
    return f"linked into {session}:{LINK_INDEX}"


def launch(config: Config, session: str | None) -> list[str]:
    ensure_agent_dir(config)
    report = []
    if has_session(SESSION):
        window = gopher_window()
        report.append(f"gopher session exists ({window})")
    else:
        window = create_session(config)
        report.append(f"started gopher session ({window})")
    target = session or current_session()
    if target and target != SESSION:
        report.append(link(window, target))
    return report
