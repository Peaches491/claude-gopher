import fnmatch
import os
import shlex
import subprocess
import sys
import termios
import tty
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from claude_gopher.checkouts import add_checkout, owners, project_dirs, read_checkouts
from claude_gopher.config import Config
from claude_gopher.inbox import SESSION_KICKOFF
from claude_gopher.prompt import render
from claude_gopher.projects import project_info
from claude_gopher.roots import PROJECT_NAME, create_project

NEW_PROJECT = "+ new project"
MENU_KEYS = "123456789abcdefghijklmnopqrstuvwxyz"
ABORT_KEYS = ("\x1b", "\x03", "\x04", "q")
EVAL_ENV = "GOPHER_SHELL_EVAL"


class WorkonError(Exception):
    pass


@dataclass(frozen=True)
class Agent:
    name: str
    command: list[str]
    resume_flags: list[str]
    kickoff: bool = True


DEFAULT_AGENTS = {
    "claude": Agent("claude", ["claude"], ["--resume", "-r", "--continue", "-c"]),
}


def workon_settings(config: Config) -> dict:
    return config.settings.get("workon", {})


def agents(config: Config) -> dict[str, Agent]:
    found = dict(DEFAULT_AGENTS)
    for name, spec in config.settings.get("agents", {}).items():
        base = found.get(name)
        found[name] = Agent(
            name=name,
            command=list(spec.get("command", base.command if base else [name])),
            resume_flags=list(spec.get("resume_flags", base.resume_flags if base else [])),
            kickoff=bool(spec.get("kickoff", True)),
        )
    return found


def pick_agent(config: Config, name: str | None) -> Agent:
    available = agents(config)
    name = name or workon_settings(config).get("default_agent", "claude")
    if name not in available:
        raise WorkonError(f"unknown agent {name!r}; configured: {', '.join(available)}")
    return available[name]


def expand(path: str) -> Path:
    return Path(path).expanduser()


def setup_commands(config: Config, project: str, checkout: Path | None) -> list[str]:
    if checkout is None:
        return []
    values = {"path": str(checkout), "name": checkout.name, "project": project, "root": str(config.root)}
    commands = []
    for entry in config.settings.get("checkouts", {}).get("setup", []):
        pattern = str(expand(entry.get("match", "*")))
        if fnmatch.fnmatch(str(checkout), pattern):
            commands.extend(render(command, values) for command in entry.get("commands", []))
    return commands


def git_toplevel(cwd: str) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def here_checkout(config: Config, cwd: str) -> Path | None:
    top = git_toplevel(cwd)
    if top is None or top == config.root or config.root in top.parents:
        return None
    return top


class TtyUI:
    def say(self, text: str) -> None:
        print(text, file=sys.stderr)

    def key(self) -> str:
        with open("/dev/tty", "rb", buffering=0) as tty_in:
            fd = tty_in.fileno()
            saved = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                mode = termios.tcgetattr(fd)
                mode[3] &= ~termios.ISIG
                termios.tcsetattr(fd, termios.TCSANOW, mode)
                return os.read(fd, 1).decode(errors="replace")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def menu(self, prompt: str, options: list[str]) -> str:
        if len(options) > len(MENU_KEYS):
            raise WorkonError(f"too many options for a menu: {len(options)}")
        keys = MENU_KEYS[: len(options)]
        for key, option in zip(keys, options):
            self.say(f"{key}) {option}")
        print(f"{prompt} [{keys[0]}-{keys[-1]}, q to abort]: ", end="", file=sys.stderr, flush=True)
        while True:
            key = self.key()
            if key in ABORT_KEYS:
                self.say("")
                raise WorkonError("aborted")
            if key in keys:
                self.say(key)
                return options[keys.index(key)]

    def confirm(self, prompt: str) -> bool:
        print(f"{prompt} [Y/n]: ", end="", file=sys.stderr, flush=True)
        while True:
            key = self.key()
            if key in ("y", "Y", "\n", "\r"):
                self.say("y")
                return True
            if key in ("n", "N") or key in ABORT_KEYS:
                self.say("n")
                return False

    def read(self, prompt: str) -> str:
        with open("/dev/tty") as tty_in:
            print(f"{prompt}: ", end="", file=sys.stderr, flush=True)
            return tty_in.readline().strip()


def require_project(config: Config, project: str) -> None:
    if not (config.projects_dir / project).is_dir():
        raise WorkonError(f"unknown project {project!r} in {config.projects_dir}")


def all_projects(config: Config) -> list[str]:
    return [d.name for d in project_dirs(config.projects_dir)]


def choose_project(config: Config, ui, candidates: list[str]) -> str:
    choice = ui.menu("Choose a project", [*candidates, NEW_PROJECT])
    if choice != NEW_PROJECT:
        return choice
    name = ui.read("New project name")
    if not PROJECT_NAME.match(name):
        raise WorkonError(f"invalid project name {name!r}")
    create_project(config.projects_dir, name)
    ui.say(f"created project {name}")
    return name


def offer_link(config: Config, ui, project: str, here: Path) -> bool:
    if here in read_checkouts(config.projects_dir / project):
        return True
    if ui.confirm(f"Add {here} to {project}'s checkouts?"):
        add_checkout(config.projects_dir, project, here)
        ui.say(f"{project} now owns {here}")
        return True
    return False


def resolve_project(config: Config, ui, project: str | None, cwd: str) -> str:
    if project:
        require_project(config, project)
        return project
    here = here_checkout(config, cwd)
    candidates = owners(config.projects_dir, here) if here else []
    if len(candidates) == 1:
        return candidates[0]
    return choose_project(config, ui, candidates or all_projects(config))


def default_checkout(config: Config, project: str) -> Path | None:
    value = workon_settings(config).get("default_checkouts", {}).get(project)
    return expand(value) if value else None


def resolve_checkout(config: Config, ui, project: str, name: str | None, cwd: str) -> Path | None:
    owned = read_checkouts(config.projects_dir / project)
    if name:
        for checkout in owned:
            if name in (checkout.name, str(checkout)):
                return checkout
        candidate = expand(name).resolve()
        if candidate.is_dir():
            return candidate
        raise WorkonError(f"{name!r} is not a checkout of {project}")
    if default := default_checkout(config, project):
        return default
    here = here_checkout(config, cwd)
    if here in owned:
        return here
    existing = [checkout for checkout in owned if checkout.is_dir()]
    if len(existing) == 1:
        return existing[0]
    if existing:
        labels = {f"{c.name}  ({c})": c for c in existing}
        return labels[ui.menu("Choose a checkout", list(labels))]
    if here and offer_link(config, ui, project, here):
        return here
    return None


def workdir(config: Config, project: str, checkout: Path | None) -> Path:
    return checkout if checkout else config.projects_dir / project


def source_lines(config: Config, project: str, checkout: Path | None) -> list[str]:
    lines = [
        f"cd {shlex.quote(str(workdir(config, project, checkout)))}",
        f"export GOPHER_HOME={shlex.quote(str(config.root))} GOPHER_PROJECT={shlex.quote(project)}",
        f'[ -n "${{TMUX_PANE:-}}" ] && tmux rename-window -t "$TMUX_PANE" {shlex.quote(project)}',
    ]
    lines += setup_commands(config, project, checkout)
    lines += workon_settings(config).get("pre_launch", [])
    return lines


def agent_argv(agent: Agent, passthrough: list[str], kickoff: str = SESSION_KICKOFF) -> list[str]:
    resuming = any(
        arg == flag or arg.startswith(f"{flag}=") for arg in passthrough for flag in agent.resume_flags
    )
    argv = [*agent.command, *passthrough]
    if agent.kickoff and not resuming:
        argv.append(kickoff)
    return argv


def agent_lines(config: Config, project: str, checkout: Path | None, agent: Agent, passthrough: list[str]) -> list[str]:
    return [*source_lines(config, project, checkout), shlex.join(agent_argv(agent, passthrough))]


def target_words(project: str, checkout: Path | None) -> list[str]:
    return [project, str(checkout)] if checkout else [project]


def status_editor_command(project_dir: Path) -> list[str]:
    editor = os.environ.get("EDITOR", "vim")
    argv = shlex.split(editor)
    if Path(argv[0]).name in ("vim", "nvim", "vi"):
        with resources.as_file(resources.files("claude_gopher") / "shell" / "status_editor.vim") as vimrc:
            argv += ["-S", str(vimrc)]
    return [*argv, str(project_dir / "human" / "STATUS.md")]


def tmux(*argv: str) -> str:
    return subprocess.run(["tmux", *argv], check=True, capture_output=True, text=True).stdout.strip()


def send(pane: str, command: str) -> None:
    tmux("send-keys", "-t", pane, command, "Enter")


def term_layout(config: Config, ui, project: str, checkout: Path | None, agent: Agent, passthrough: list[str]) -> list[str] | None:
    if not os.environ.get("TMUX"):
        raise WorkonError("term must run inside tmux")
    directory = str(workdir(config, project, checkout))
    base = os.environ["TMUX_PANE"]
    in_current_pane = True
    if int(tmux("display-message", "-p", "-t", base, "#{window_panes}")) > 1:
        choice = ui.menu("This window has other panes", ["Replace this window's panes", "Open a new window", "Abort"])
        if choice == "Abort":
            raise WorkonError("aborted")
        if choice.startswith("Replace"):
            tmux("kill-pane", "-a", "-t", base)
        else:
            base = tmux("new-window", "-P", "-F", "#{pane_id}", "-n", project, "-c", directory)
            in_current_pane = False
    project_dir = config.projects_dir / project
    (project_dir / "human").mkdir(exist_ok=True)
    agent_pane = tmux("split-window", "-h", "-l", "70%", "-t", base, "-c", directory, "-P", "-F", "#{pane_id}")
    editor_pane = tmux("split-window", "-v", "-b", "-l", "30%", "-t", base, "-c", str(project_dir), "-P", "-F", "#{pane_id}")
    send(editor_pane, shlex.join(status_editor_command(project_dir)))
    agent_cmd = ["gopher", "--root", str(config.root), "workon", "agent", "--agent", agent.name, *target_words(project, checkout)]
    if passthrough:
        agent_cmd += ["--", *passthrough]
    send(agent_pane, shlex.join(agent_cmd))
    tmux("select-pane", "-t", agent_pane)
    if in_current_pane and os.environ.get(EVAL_ENV):
        return source_lines(config, project, checkout)
    send(base, shlex.join(["gopher", "--root", str(config.root), "workon", "source", *target_words(project, checkout)]))
    return None


def run_lines(lines: list[str]) -> None:
    shell = os.environ.get("SHELL") or "/bin/sh"
    script = "\n".join([*lines[:-1], f"exec {lines[-1]}"])
    os.execvp(shell, [shell, "-c", script])


def project_summary(config: Config, project: str) -> str:
    return project_info(config.projects_dir / project)["summary"][:70]


ACTIONS = {
    "agent": "start an agent for a project in this terminal",
    "term": "lay out this tmux window: STATUS editor, shell, and agent",
    "source": "cd to a project's checkout and activate it (needs gopher shell-init)",
    "where": "list projects and their checkouts",
    "link": "add the current checkout to a project",
    "unlink": "remove the current checkout from a project",
}


def completions(config: Config, words: list[str]) -> list[tuple[str, str]]:
    if not words:
        return list(ACTIONS.items())
    action = words[0]
    rest = [w for i, w in enumerate(words[1:], 1) if not w.startswith("-") and words[i - 1] != "--agent"]
    projects = all_projects(config)
    project_items = [(p, project_summary(config, p)) for p in projects]
    if action in ("where", "link", "unlink"):
        return project_items if not rest else []
    if action not in ("agent", "term", "source"):
        return []
    if words[-1] == "--agent":
        return [(name, " ".join(agent.command)) for name, agent in agents(config).items()]
    if not rest:
        return project_items
    if len(rest) == 1 and rest[0] in projects:
        items = [(c.name, str(c)) for c in read_checkouts(config.projects_dir / rest[0])]
        if (default := default_checkout(config, rest[0])) and default.name not in dict(items):
            items.insert(0, (str(default), "default"))
        return items
    return []


SHELL_INIT = {
    "bash": """gopher() {
  if [ "${1:-}" = workon ] && { [ "${2:-}" = source ] || [ "${2:-}" = agent ] || [ "${2:-}" = term ]; }; then
    local code
    code="$(GOPHER_SHELL_EVAL=1 command gopher "$@")" || return
    eval "$code"
  else
    command gopher "$@"
  fi
}

_gopher_complete() {
  local cur="${COMP_WORDS[COMP_CWORD]}"
  local IFS=$'\\n'
  if [ "${COMP_WORDS[1]:-}" = workon ] && [ "$COMP_CWORD" -ge 2 ]; then
    COMPREPLY=($(compgen -W "$(command gopher workon complete -- "${COMP_WORDS[@]:2:COMP_CWORD-2}" 2>/dev/null)" -- "$cur"))
  elif [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "$(command gopher commands 2>/dev/null)" -- "$cur"))
  fi
}
complete -o nosort -F _gopher_complete gopher
""",
    "zsh": """gopher() {
  if [[ "${1:-}" == workon && "${2:-}" == (source|agent|term) ]]; then
    local code
    code="$(GOPHER_SHELL_EVAL=1 command gopher "$@")" || return
    eval "$code"
  else
    command gopher "$@"
  fi
}

_gopher() {
  local -a items
  if (( CURRENT == 2 )); then
    items=("${(@f)$(command gopher commands --describe 2>/dev/null)}")
    items=(${items:#})
    _describe -t commands command items
  elif [[ ${words[2]} == workon ]]; then
    items=("${(@f)$(command gopher workon complete --zsh -- "${(@)words[3,CURRENT-1]}" 2>/dev/null)}")
    items=(${items:#})
    (( ${#items} )) && _describe -t values value items
  fi
}
(( $+functions[compdef] )) && compdef _gopher gopher
""",
}
