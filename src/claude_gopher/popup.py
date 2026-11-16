import shlex
import subprocess
import time
from pathlib import Path

GOPHER_SESSION = "gopher"
PEEK_PREFIX = "gopher-peek-"
CLIENT_OPTION = "@gopher_popup_client"
ORIGIN_OPTION = "@gopher_popup_pane"
TARGET_OPTION = "@gopher_popup_target"
AUTOCLOSE_OPTION = "@gopher_popup_autoclose"
OPTIONS = (CLIENT_OPTION, ORIGIN_OPTION, TARGET_OPTION, AUTOCLOSE_OPTION)
DIM_STYLE = "dim=40"
SIZE = ("90%", "90%")


def tmux(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *argv], capture_output=True, text=True)


def value(*argv: str) -> str:
    result = tmux(*argv)
    return result.stdout.strip() if result.returncode == 0 else ""


def is_popup_session(name: str) -> bool:
    return name == GOPHER_SESSION or name.startswith(PEEK_PREFIX)


def client_for_pane(pane: str) -> str | None:
    window = value("display-message", "-p", "-t", pane, "#{window_id}")
    for line in value("list-clients", "-F", "#{client_name}\t#{window_id}\t#{session_name}").splitlines():
        client, client_window, session = (line.split("\t") + ["", ""])[:3]
        if client_window == window and not is_popup_session(session):
            return client
    return None


def gopher_agent_pane() -> str | None:
    for line in value("list-panes", "-s", "-t", f"={GOPHER_SESSION}:", "-F", "#{pane_id}\t#{pane_current_command}").splitlines():
        pane, _, command = line.partition("\t")
        if command == "claude":
            return pane
    return None


def exact_target(session: str) -> str:
    return "'=" + session.replace("'", "'\\''") + "'"


def attach_command(target: str, kind: str) -> str:
    if kind == "gopher":
        return f"TMUX= tmux attach -t {exact_target(GOPHER_SESSION)}"
    session = value("display-message", "-p", "-t", target, "#{session_name}")
    window = value("display-message", "-p", "-t", target, "#{window_id}")
    peek = f"{PEEK_PREFIX}{int(time.time() * 1000)}"
    return (
        f"TMUX= tmux new-session -t {exact_target(session)} -s {peek} \\; "
        f"set-option destroy-unattached on \\; select-window -t {window}"
    )


def cleanup_command(socket: str, origin_window: str, target: str, unzoom: bool) -> str:
    tm = f"tmux -S {shlex.quote(socket)}"
    steps = [f"{tm} set -wu -t {origin_window} window-style"]
    steps += [f"{tm} set -gu {option}" for option in OPTIONS]
    if unzoom:
        steps.append(f"{tm} if -F -t {target} '#{{window_zoomed_flag}}' 'resize-pane -Z -t {target}'")
    return "; ".join(steps)


def open_popup(kind: str, origin: str, target: str | None = None, prefill: str = "", client: str | None = None) -> str:
    if kind == "gopher":
        target = gopher_agent_pane()
        if not target:
            raise RuntimeError("Gopher is not running; start it with gopher launch")
    elif not target:
        raise RuntimeError("no agent pane to open")
    client = client or client_for_pane(origin)
    if not client:
        raise RuntimeError("no tmux client is showing this pane")
    socket = value("display-message", "-p", "#{socket_path}")
    origin_window = value("display-message", "-p", "-t", origin, "#{window_id}")
    unzoom = False
    if kind == "agent" and value("display-message", "-p", "-t", target, "#{window_zoomed_flag}") != "1":
        tmux("resize-pane", "-Z", "-t", target)
        unzoom = True
    tmux("set", "-w", "-t", origin_window, "window-style", DIM_STYLE)
    for option, setting in ((CLIENT_OPTION, client), (ORIGIN_OPTION, origin), (TARGET_OPTION, target)):
        tmux("set", "-g", option, setting)
    if prefill:
        tmux("set", "-g", AUTOCLOSE_OPTION, "1")
        tmux("send-keys", "-t", target, "-l", prefill)
    command = f"{attach_command(target, kind)}; {cleanup_command(socket, origin_window, target, unzoom)}"
    width, height = SIZE
    subprocess.Popen(
        ["tmux", "display-popup", "-c", client, "-E", "-s", "bg=terminal", "-S", "bg=terminal",
         "-w", width, "-h", height, command],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    return target


CLOSE_PROMPT = 'status=$?; printf "\\n(exit %s; press enter to close)" "$status"; read -r _'


def command_popup(origin: str, cwd: Path | str, command: str, env: dict[str, str], title: str = "", client: str | None = None) -> None:
    client = client or client_for_pane(origin)
    if not client:
        raise RuntimeError("no tmux client is showing this pane")
    socket = value("display-message", "-p", "#{socket_path}")
    origin_window = value("display-message", "-p", "-t", origin, "#{window_id}")
    tmux("set", "-w", "-t", origin_window, "window-style", DIM_STYLE)
    script = f"sh -c {shlex.quote(f'{command}; {CLOSE_PROMPT}')}; tmux -S {shlex.quote(socket)} set -wu -t {origin_window} window-style"
    env_args = [arg for key, setting in env.items() for arg in ("-e", f"{key}={setting}")]
    width, height = SIZE
    subprocess.Popen(
        ["tmux", "display-popup", "-c", client, "-E", "-d", str(cwd), *env_args, "-T", title,
         "-s", "bg=terminal", "-S", "bg=terminal", "-w", width, "-h", height, script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def popup_clients() -> list[str]:
    clients = []
    for line in value("list-clients", "-F", "#{client_name}\t#{session_name}").splitlines():
        client, _, session = line.partition("\t")
        if is_popup_session(session):
            clients.append(client)
    return clients


def close_popup() -> None:
    for client in popup_clients():
        tmux("detach-client", "-t", client)


def autoclose_after_submit(pane: str | None) -> bool:
    if not pane or value("show", "-gv", AUTOCLOSE_OPTION) != "1" or value("show", "-gv", TARGET_OPTION) != pane:
        return False
    tmux("set", "-gu", AUTOCLOSE_OPTION)
    close_popup()
    return True


def jump_to(pane: str) -> None:
    outer = value("show", "-gv", CLIENT_OPTION)
    nested = popup_clients()
    if outer and nested:
        tmux("switch-client", "-c", outer, "-t", pane)
        close_popup()
    else:
        tmux("switch-client", "-t", pane)
