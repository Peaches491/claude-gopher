import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from claude_gopher.checkouts import add_checkout, all_checkouts, owners, project_dirs, read_checkouts, remove_checkout
from claude_gopher.inbox import PROMPT, RESET_SOURCES, START, Tracker, add_item, inbox_context, inbox_notice, unread, wake_reason
from claude_gopher.config import (
    PROACTIVITY_LEVELS, ROOT_CONFIG, ConfigError, gopher_proactivity, load_config, load_registry, resolve_root,
    save_registry, set_gopher_setting,
)
from claude_gopher.projects import list_projects, project_info
from claude_gopher.prompt import CONTEXTS, assemble, is_gopher_session, session_context
from claude_gopher.roots import RootError, check_name, check_target, create_project, create_root, register_root
from claude_gopher.overview import current_sessions, project_overview
from claude_gopher.setup import SetupIO, launch_interview, run_setup
from claude_gopher import launch, popup, rename, workon
from claude_gopher.state import (
    apply_event,
    hook_settings,
    location_label,
    resolve_project,
)


def cmd_hook_state(args) -> int:
    if args.print_settings:
        print(json.dumps(hook_settings(), indent=2))
        return 0
    config = None
    try:
        payload = json.load(sys.stdin)
        root = resolve_root(env=os.environ, cwd=payload.get("cwd"))
        if not (root / ROOT_CONFIG).is_file():
            return 0
        config = load_config(root)
        apply_event(payload, config.sessions_dir, config.projects_dir, dict(os.environ))
        if payload.get("hook_event_name") == "UserPromptSubmit" and os.environ.get("TMUX"):
            popup.autoclose_after_submit(os.environ.get("TMUX_PANE"))
    except Exception:
        log_error(config, "hook state")
    return 0


def log_error(config, what: str) -> None:
    log_dir = config.state_dir if config else Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "claude-gopher"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "errors.log", "a") as log:
        log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {what}\n{traceback.format_exc()}\n")


def context_hook_settings(command: str = "gopher hook context") -> dict:
    return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}


def hook_session(payload: dict):
    cwd = payload.get("cwd") or ""
    root = resolve_root(env=os.environ, cwd=cwd)
    if not (root / ROOT_CONFIG).is_file():
        return None, None, False
    config = load_config(root)
    env = dict(os.environ)
    if is_gopher_session(config, cwd, env):
        return config, None, True
    return config, resolve_project(cwd, config.projects_dir, env), False


def project_inbox_context(config, project: str, payload: dict) -> tuple[str, str]:
    project_dir = config.projects_dir / project
    tracker = Tracker(config.state_dir, project, payload.get("session_id") or "unknown")
    starting = payload.get("hook_event_name") == "SessionStart"
    if starting and payload.get("source") in RESET_SOURCES:
        tracker.reset("seen")
    items = unread(project_dir)
    seen = tracker.get("seen")
    text = inbox_context(project_dir, items, seen, START if starting else PROMPT)
    notice = inbox_notice(project, items, seen)
    tracker.add("seen", {item.name for item in items})
    return text, notice


def emit(event: str, text: str, notice: str | None = None) -> None:
    output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if notice:
        output["systemMessage"] = notice
    print(json.dumps(output))


def cmd_hook_context(args) -> int:
    if args.print_settings:
        print(json.dumps(context_hook_settings(), indent=2))
        return 0
    config = None
    try:
        payload = json.load(sys.stdin)
        config, project, is_gopher = hook_session(payload)
        if config is None:
            return 0
        notice = None
        if is_gopher:
            prompt_file = os.environ.get(launch.PROMPT_ENV)
            if prompt_file and Path(prompt_file).is_file():
                text = f"# Gopher\n\nYou are Gopher; your instructions are in your system prompt (from `{prompt_file}`)."
            else:
                text = session_context(config, "gopher", None)
            notice = "gopher: Gopher session"
        elif project:
            text = session_context(config, "project", project)
            inbox, notice = project_inbox_context(config, project, payload)
            if inbox:
                text += "\n" + inbox
        else:
            return 0
        emit("SessionStart", text, notice)
    except Exception:
        log_error(config, "hook context")
    return 0


def cmd_hook_inbox(args) -> int:
    config = None
    try:
        payload = json.load(sys.stdin)
        config, project, _ = hook_session(payload)
        if config is None or not project or not (config.projects_dir / project).is_dir():
            return 0
        event = payload.get("hook_event_name")
        if event == "UserPromptSubmit":
            text, _ = project_inbox_context(config, project, payload)
            if text:
                emit(event, text)
        elif event == "Stop":
            project_dir = config.projects_dir / project
            tracker = Tracker(config.state_dir, project, payload.get("session_id") or "unknown")
            reason, names = wake_reason(project_dir, unread(project_dir), tracker.get("woken"))
            if reason:
                tracker.add("woken", names)
                tracker.add("seen", names)
                print(json.dumps({"decision": "block", "reason": reason}))
    except Exception:
        log_error(config, "hook inbox")
    return 0


def cmd_inbox_add(args) -> int:
    config = root_config(args)
    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not text.strip():
        raise RootError("inbox item text is empty")
    print(add_item(config.projects_dir / args.project, text, sender=args.sender, wake=args.wake))
    return 0


def cmd_inbox_list(args) -> int:
    config = root_config(args)
    names = [args.project] if args.project else [d.name for d in project_dirs(config.projects_dir)]
    rows = []
    for name in names:
        for item in unread(config.projects_dir / name):
            first = item.body.splitlines()[0] if item.body else ""
            rows.append((name, "wake" if item.wake else "-", item.sender, item.created, first[:70]))
    if rows:
        print_table(("PROJECT", "WAKE", "FROM", "CREATED", "TEXT"), rows)
    return 0


def cmd_prompt(args) -> int:
    config = root_config(args)
    project = args.project or resolve_project(os.getcwd(), config.projects_dir, dict(os.environ))
    print(assemble(config, args.context, project), end="")
    return 0


def root_config(args):
    return load_config(resolve_root(args.root))


def ask(prompt: str, default: str | None = None) -> str:
    if not sys.stdin.isatty():
        raise RootError(f"{prompt.rstrip(': ')} is required (no terminal to prompt on)")
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default or ""


def cmd_new(args) -> int:
    target = Path(args.dir or ask("Root directory", "~/workon")).expanduser().resolve()
    check_target(target)
    registry = load_registry()
    name = args.name or ask("Root name", target.name)
    check_name(name, target, registry)
    make_default = args.default
    if make_default is None and registry.default and registry.default != name:
        make_default = ask(f"Make {name!r} the default root (current: {registry.default})? y/N", "n").lower().startswith("y")
    create_root(target, args.source)
    register_root(registry, name, target, bool(make_default))
    save_registry(registry)
    print(f"created root {name!r} at {target}" + (" (default)" if registry.default == name else ""))
    return 0


def confirm(prompt: str, default: bool) -> bool:
    if not sys.stdin.isatty():
        raise RootError(f"{prompt} needs an answer but there is no terminal; rerun with -y (or the matching --no-* flag)")
    answer = ask(f"{prompt} {'Y/n' if default else 'y/N'}", "y" if default else "n")
    return answer.lower().startswith("y")


def cmd_setup(args) -> int:
    return run_setup(args, SetupIO(ask=ask, confirm=confirm, say=print, launch=launch_interview))


def workon_target(args, config, ui):
    project = workon.resolve_project(config, ui, args.project, os.getcwd())
    return project, workon.resolve_checkout(config, ui, project, args.checkout, os.getcwd())


def cmd_workon_source(args) -> int:
    config = root_config(args)
    if not os.environ.get(workon.EVAL_ENV):
        raise workon.WorkonError('source changes your shell; load the shell function first: eval "$(gopher shell-init zsh)"')
    project, checkout = workon_target(args, config, workon.TtyUI())
    print("\n".join(workon.source_lines(config, project, checkout)))
    return 0


def cmd_workon_agent(args) -> int:
    config = root_config(args)
    agent = workon.pick_agent(config, args.agent)
    project, checkout = workon_target(args, config, workon.TtyUI())
    lines = workon.agent_lines(config, project, checkout, agent, args.passthrough)
    if os.environ.get(workon.EVAL_ENV):
        print("\n".join(lines))
    else:
        workon.run_lines(lines)
    return 0


def cmd_launch(args) -> int:
    config = root_config(args)
    if args.exec_agent:
        launch.exec_agent(config, args.passthrough)
        return 0
    for line in launch.launch(config, args.session):
        print(f"gopher: {line}")
    return 0


def cmd_tui(args) -> int:
    from claude_gopher import tui  # deferred: textual import is slow and only the TUI needs it

    tui.run(root_config(args), project=args.project)
    return 0


def cmd_popup(args) -> int:
    origin = args.origin or os.environ.get("TMUX_PANE")
    if not origin:
        raise RootError("gopher popup must run inside tmux or get --origin")
    try:
        popup.open_popup(args.kind, origin, target=args.target, prefill=args.prefill, client=args.client)
    except RuntimeError as error:
        raise RootError(str(error)) from error
    return 0


def cmd_proactivity(args) -> int:
    config = root_config(args)
    if args.level:
        set_gopher_setting(config.root, "proactivity", args.level)
        config = load_config(config.root)
    print(gopher_proactivity(config))
    return 0


def cmd_workon_term(args) -> int:
    config = root_config(args)
    ui = workon.TtyUI()
    agent = workon.pick_agent(config, args.agent)
    project, checkout = workon_target(args, config, ui)
    lines = workon.term_layout(config, ui, project, checkout, agent, args.passthrough)
    if lines:
        print("\n".join(lines))
    return 0


def cmd_workon_sidebar(args) -> int:
    config = root_config(args)
    if not os.environ.get("TMUX"):
        raise RootError("gopher workon sidebar must run inside tmux")
    if not args.all and not args.project:
        raise RootError("name a project or pass --all")
    windows = workon.project_windows(config, None if args.all else args.project)
    if not windows:
        print("no open project windows matched")
    for window, project in windows:
        print(workon.add_sidebar(config, window, project, replace=args.replace))
    return 0


def cmd_workon_where(args) -> int:
    config = root_config(args)
    names = [args.project] if args.project else [d.name for d in project_dirs(config.projects_dir)]
    for name in names:
        workon.require_project(config, name)
        checkouts = read_checkouts(config.projects_dir / name)
        if args.project or checkouts:
            print(name)
            for checkout in checkouts:
                print(f"  {checkout}{'' if checkout.is_dir() else '  (missing)'}")
    return 0


def current_checkout(config) -> Path:
    here = workon.here_checkout(config, os.getcwd())
    if here is None:
        raise workon.WorkonError(f"{os.getcwd()} is not inside a git checkout outside the root")
    return here


def cmd_workon_link(args) -> int:
    config = root_config(args)
    workon.require_project(config, args.project)
    here = current_checkout(config)
    add_checkout(config.projects_dir, args.project, here)
    print(f"{args.project} now owns {here}", file=sys.stderr)
    return 0


def cmd_workon_unlink(args) -> int:
    config = root_config(args)
    here = current_checkout(config)
    if not remove_checkout(config.projects_dir, args.project, here):
        raise workon.WorkonError(f"{here} is not a checkout of {args.project}")
    print(f"{args.project} no longer owns {here}", file=sys.stderr)
    return 0


def cmd_workon_complete(args) -> int:
    try:
        config = root_config(args)
    except (ConfigError, OSError):
        return 0
    for value, description in workon.completions(config, args.words):
        if args.zsh:
            value = value.replace(":", r"\:")
            print(f"{value}:{description}" if description else value)
        else:
            print(value)
    return 0


def cmd_shell_init(args) -> int:
    print(workon.SHELL_INIT[args.shell], end="")
    return 0


def cmd_commands(args) -> int:
    for name, description in COMMANDS.items():
        print(f"{name}:{description}" if args.describe else name)
    return 0


COMMANDS: dict = {}


def cmd_project_new(args) -> int:
    config = root_config(args)
    project_dir = create_project(config.projects_dir, args.name, args.summary)
    for checkout in args.checkout:
        add_checkout(config.projects_dir, args.name, Path(checkout).expanduser().resolve())
    print(project_dir)
    return 0


def cmd_project_rename(args) -> int:
    config = root_config(args)
    report = rename.rename_project(config, args.old, args.new, windows=not args.no_windows)
    print(f"renamed {args.old} -> {args.new}")
    for where in report.windows:
        print(f"  tmux window {where} renamed")
    if report.sessions:
        print(f"  {len(report.sessions)} session record(s) updated; running agents keep GOPHER_PROJECT={args.old}"
              " and resolve the project from their checkout until relaunched")
    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    return 0


def cmd_checkout_add(args) -> int:
    config = root_config(args)
    path = Path(args.path).expanduser().resolve()
    add_checkout(config.projects_dir, args.project, path)
    others = [o for o in owners(config.projects_dir, path) if o != args.project]
    if others:
        print(f"note: {path} is also owned by {', '.join(others)}", file=sys.stderr)
    return 0


def cmd_checkout_remove(args) -> int:
    config = root_config(args)
    path = Path(args.path).expanduser().resolve()
    if not remove_checkout(config.projects_dir, args.project, path):
        print(f"{path} is not a checkout of {args.project}", file=sys.stderr)
        return 1
    return 0


def cmd_checkout_move(args) -> int:
    config = root_config(args)
    path = Path(args.path).expanduser().resolve()
    if not (config.projects_dir / args.project).is_dir():
        raise FileNotFoundError(f"no project {args.project!r} in {config.projects_dir}")
    for owner in owners(config.projects_dir, path):
        if owner != args.project:
            remove_checkout(config.projects_dir, owner, path)
    add_checkout(config.projects_dir, args.project, path)
    return 0


def cmd_checkout_list(args) -> int:
    config = root_config(args)
    print_table(("PROJECT", "CHECKOUT"), [(name, str(path)) for name, path in all_checkouts(config.projects_dir)])
    return 0


def format_age(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def print_table(header: tuple, rows: list[tuple], show_header: bool = True) -> None:
    shown = [header, *rows] if show_header else rows
    widths = [max(len(str(row[i])) for row in shown) for i in range(len(header) - 1)]
    for row in shown:
        cells = [str(cell).ljust(width) for cell, width in zip(row, widths)]
        print("  ".join([*cells, str(row[-1])]).rstrip())


def cmd_agents(args) -> int:
    config = root_config(args)
    sessions = current_sessions(config, prune=not args.no_prune)
    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0
    now = time.time()
    print_table(
        ("PROJECT", "STATE", "WINDOW", "AGE", "NAME", "CWD"),
        [
            (s.get("project") or "-", s.get("state") or "?", location_label(s.get("location")),
             format_age(now - s.get("updated", now)), s.get("name") or "-", s.get("cwd") or "")
            for s in sessions
        ],
    )
    return 0


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def cmd_projects(args) -> int:
    config = root_config(args)
    projects = project_overview(config)
    if args.json:
        print(json.dumps(projects, indent=2))
        return 0
    rows = []
    for project in projects:
        agents = ", ".join(
            " ".join(part for part in (s.get("state") or "?", location_label(s.get("location"), with_name=False)) if part != "-")
            for s in project["sessions"]
        )
        inbox = project["inbox"]
        inbox_text = "-" if not inbox["unread"] else f"{inbox['unread']}{'!' if inbox['wake'] else ''}"
        rows.append((
            project["name"], project["state"] or "-", agents or "-", inbox_text,
            str(len(project["checkouts"])), truncate(project["summary"], args.width),
        ))
    print_table(("PROJECT", "STATE", "AGENT", "INBOX", "CO", "SUMMARY"), rows)
    return 0


def cmd_list(args) -> int:
    config = root_config(args)
    projects = [project_info(directory) for directory in project_dirs(config.projects_dir)]
    if args.names:
        print("\n".join(p["name"] for p in projects))
    elif projects:
        print_table(("", ""), [(p["name"], truncate(p["summary"], args.width)) for p in projects], show_header=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gopher")
    parser.add_argument("--root", help="root name or path (default: $GOPHER_HOME, then the registry default)")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a gopher root")
    new.add_argument("dir", nargs="?", help="root directory (prompted if omitted)")
    new.add_argument("--name", help="registry name (default: directory basename)")
    new.add_argument("--from", dest="source", metavar="GIT_URL", help="clone an existing root")
    default = new.add_mutually_exclusive_group()
    default.add_argument("--default", dest="default", action="store_true", default=None)
    default.add_argument("--no-default", dest="default", action="store_false")
    new.set_defaults(func=cmd_new)

    setup = sub.add_parser("setup", help="create or set up a gopher root on this machine, then interview to configure it")
    setup.add_argument("dir", nargs="?", help="root directory (prompted if omitted)")
    setup.add_argument("--name", help="registry name (default: directory basename)")
    setup.add_argument("--from", dest="source", metavar="GIT_URL", help="clone an existing root")
    setup_default = setup.add_mutually_exclusive_group()
    setup_default.add_argument("--default", dest="default", action="store_true", default=None)
    setup_default.add_argument("--no-default", dest="default", action="store_false")
    setup.add_argument("--no-hooks", action="store_true", help="do not offer to install Claude Code hooks")
    setup.add_argument("--no-interview", action="store_true", help="do not offer the setup interview")
    setup.add_argument("--review", action="store_true", help="only run the interview, revising an existing root")
    setup.add_argument("-y", "--yes", action="store_true", help="install hooks without asking")
    setup.set_defaults(func=cmd_setup)

    launcher = sub.add_parser("launch", help="start the Gopher agent's tmux session and link its window at index 0")
    launcher.add_argument("--session", help="tmux session to link into (default: the current one)")
    launcher.add_argument("--exec-agent", action="store_true", help=argparse.SUPPRESS)
    launcher.set_defaults(func=cmd_launch, passthrough=[])

    tui_parser = sub.add_parser("tui", help="project sidebar/dashboard: agents, inbox, PRs, summary")
    tui_parser.add_argument("--project", help="pin this project in the header (default: the pane's project)")
    tui_parser.set_defaults(func=cmd_tui)
    popup_parser = sub.add_parser("popup", help="open a tmux popup on Gopher or a project's agent pane")
    popup_parser.add_argument("kind", choices=("gopher", "agent"))
    popup_parser.add_argument("--target", help="agent pane id (for kind agent)")
    popup_parser.add_argument("--origin", help="pane the popup is opened from (default: $TMUX_PANE)")
    popup_parser.add_argument("--client", help="tmux client to show it on (default: the one viewing --origin)")
    popup_parser.add_argument("--prefill", default="", help="text to type into the agent; Enter there closes the popup")
    popup_parser.set_defaults(func=cmd_popup)
    proactivity = sub.add_parser("proactivity", help="show or set how proactive Gopher is ([gopher] proactivity)")
    proactivity.add_argument("level", nargs="?", choices=PROACTIVITY_LEVELS)
    proactivity.set_defaults(func=cmd_proactivity)

    project = sub.add_parser("project", help="manage projects").add_subparsers(dest="project_command", required=True)
    project_new = project.add_parser("new", help="create a project")
    project_new.add_argument("name")
    project_new.add_argument("--summary", default="")
    project_new.add_argument("--checkout", action="append", default=[], help="checkout path owned by the project")
    project_new.set_defaults(func=cmd_project_new)
    project_rename = project.add_parser("rename", help="rename a project: directory, STATUS heading, gopher state, tmux windows")
    project_rename.add_argument("old")
    project_rename.add_argument("new")
    project_rename.add_argument("--no-windows", action="store_true", help="leave tmux window names alone")
    project_rename.set_defaults(func=cmd_project_rename)

    checkout = sub.add_parser("checkout", help="manage project checkouts").add_subparsers(dest="checkout_command", required=True)
    checkout_add = checkout.add_parser("add", help="give a project a checkout")
    checkout_add.add_argument("project")
    checkout_add.add_argument("path", nargs="?", default=".")
    checkout_add.set_defaults(func=cmd_checkout_add)
    checkout_remove = checkout.add_parser("remove", help="drop a checkout from a project")
    checkout_remove.add_argument("project")
    checkout_remove.add_argument("path", nargs="?", default=".")
    checkout_remove.set_defaults(func=cmd_checkout_remove)
    checkout_move = checkout.add_parser("move", help="transfer a checkout to a project")
    checkout_move.add_argument("path")
    checkout_move.add_argument("project")
    checkout_move.set_defaults(func=cmd_checkout_move)
    checkout_list = checkout.add_parser("list", help="list checkouts by project")
    checkout_list.set_defaults(func=cmd_checkout_list)

    hook = sub.add_parser("hook", help="Claude Code hook entry points").add_subparsers(dest="hook", required=True)
    hook_state = hook.add_parser("state", help="record session state from a hook payload on stdin")
    hook_state.add_argument("--print-settings", action="store_true", help="print the settings.json hooks block")
    hook_state.set_defaults(func=cmd_hook_state)
    hook_context = hook.add_parser("context", help="SessionStart hook: inject gopher instructions for the session's project")
    hook_context.add_argument("--print-settings", action="store_true", help="print the settings.json hooks block")
    hook_context.set_defaults(func=cmd_hook_context)

    hook_inbox = hook.add_parser("inbox", help="UserPromptSubmit/Stop hook: deliver inbox items, wake for wake: true")
    hook_inbox.set_defaults(func=cmd_hook_inbox)

    inbox = sub.add_parser("inbox", help="file and list inbox items").add_subparsers(dest="inbox_command", required=True)
    inbox_add = inbox.add_parser("add", help="file an item in a project's inbox (text from args or stdin)")
    inbox_add.add_argument("project")
    inbox_add.add_argument("text", nargs="*", help="message; read from stdin when omitted")
    inbox_add.add_argument("--wake", action="store_true", help="wake the project's agent when it next goes idle")
    inbox_add.add_argument("--from", dest="sender", default="user")
    inbox_add.set_defaults(func=cmd_inbox_add)
    inbox_list = inbox.add_parser("list", help="list unread inbox items")
    inbox_list.add_argument("project", nargs="?")
    inbox_list.set_defaults(func=cmd_inbox_list)

    workon_parser = sub.add_parser("workon", help="open a project: agent, tmux layout, or shell")
    workon_sub = workon_parser.add_subparsers(dest="workon_command", required=True)
    for action, func in (("agent", cmd_workon_agent), ("term", cmd_workon_term), ("source", cmd_workon_source)):
        target = workon_sub.add_parser(action, help=workon.ACTIONS[action])
        target.add_argument("project", nargs="?")
        target.add_argument("checkout", nargs="?", help="checkout name or path (default: the project's)")
        if action != "source":
            target.add_argument("--agent", help="agent from the root config (default: claude)")
        target.set_defaults(func=func, passthrough=[])
    sidebar = workon_sub.add_parser("sidebar", help=workon.ACTIONS["sidebar"])
    sidebar.add_argument("project", nargs="?")
    sidebar.add_argument("--all", action="store_true", help="every open project window")
    sidebar.add_argument("--replace", action="store_true", help="also close the window's shell and editor panes")
    sidebar.set_defaults(func=cmd_workon_sidebar)
    where = workon_sub.add_parser("where", help=workon.ACTIONS["where"])
    where.add_argument("project", nargs="?")
    where.set_defaults(func=cmd_workon_where)
    for action, func in (("link", cmd_workon_link), ("unlink", cmd_workon_unlink)):
        linker = workon_sub.add_parser(action, help=workon.ACTIONS[action])
        linker.add_argument("project")
        linker.set_defaults(func=func)
    complete = workon_sub.add_parser("complete", help="shell completion backend")
    complete.add_argument("--zsh", action="store_true")
    complete.set_defaults(func=cmd_workon_complete, words=[])

    shell_init = sub.add_parser("shell-init", help="print shell integration: a gopher function (for workon source) and completion")
    shell_init.add_argument("shell", choices=sorted(workon.SHELL_INIT))
    shell_init.set_defaults(func=cmd_shell_init)

    commands = sub.add_parser("commands", help="list gopher commands (for completion)")
    commands.add_argument("--describe", action="store_true")
    commands.set_defaults(func=cmd_commands)

    prompt = sub.add_parser("prompt", help="print the assembled prompt for a context")
    prompt.add_argument("context", choices=CONTEXTS)
    prompt.add_argument("--project", help="project to fill in (default: $GOPHER_PROJECT or the cwd's checkout)")
    prompt.set_defaults(func=cmd_prompt)

    agents = sub.add_parser("agents", help="list running agents")
    agents.add_argument("--json", action="store_true")
    agents.add_argument("--no-prune", action="store_true", help="do not delete stale records")
    agents.set_defaults(func=cmd_agents)

    listing = sub.add_parser("list", help="list projects: name and summary")
    listing.add_argument("--names", action="store_true", help="names only, one per line")
    listing.add_argument("--width", type=int, default=80, help="truncate summaries to this width")
    listing.set_defaults(func=cmd_list)

    projects = sub.add_parser("projects", help="project overview: state, agents, inbox, checkouts, summary")
    projects.add_argument("--json", action="store_true")
    projects.add_argument("--width", type=int, default=80, help="truncate summaries to this width")
    projects.set_defaults(func=cmd_projects)
    COMMANDS.update({choice.dest: choice.help or "" for choice in sub._choices_actions})
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = None
    if "--" in argv:
        split = argv.index("--")
        argv, tail = argv[:split], argv[split + 1 :]
    args, extra = parser.parse_known_args(argv)
    if tail is not None:
        if getattr(args, "func", None) in (cmd_workon_agent, cmd_workon_term, cmd_launch):
            args.passthrough = tail
        elif getattr(args, "func", None) is cmd_workon_complete:
            args.words = tail
        else:
            parser.error("'--' is only accepted by launch, workon agent, workon term, and workon complete")
    if extra:
        if getattr(args, "func", None) is not cmd_inbox_add or any(word.startswith("-") for word in extra):
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
        args.text = [*args.text, *extra]
    try:
        return args.func(args)
    except subprocess.CalledProcessError as error:
        print(f"gopher: {' '.join(error.cmd)} failed: {(error.stderr or '').strip()}", file=sys.stderr)
        return 1
    except (ConfigError, RootError, FileNotFoundError, workon.WorkonError) as error:
        print(f"gopher: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
