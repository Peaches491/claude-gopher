import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from claude_gopher.checkouts import add_checkout, all_checkouts, all_shared, owners, project_dirs, read_checkouts, remove_checkout
from claude_gopher.inbox import PROMPT, RESET_SOURCES, START, Tracker, add_item, inbox_context, inbox_notice, unread, wake_reason
from claude_gopher.config import (
    PROACTIVITY_LEVELS, ROOT_CONFIG, ConfigError, gopher_proactivity, load_config, load_registry, resolve_root,
    save_registry, set_gopher_setting,
)
from claude_gopher.projects import MAX_PRIORITY, read_priority, write_priority
from claude_gopher.prompt import CONTEXTS, assemble, is_gopher_session, session_context
from claude_gopher.roots import RootError, check_name, check_target, create_project, create_root, register_root
from claude_gopher.overview import current_sessions, project_overview
from claude_gopher.setup import SetupIO, launch_interview, run_setup
from claude_gopher import bugreport, clean, devreload, doctor, launch, lifecycle, popup, prsync, rename, restore, rootgit, schedule, update, workon
from claude_gopher.state import (
    apply_event,
    mark_needs_login,
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
        config, project, gopher = hook_session(payload)
        event = payload.get("hook_event_name")
        if config is not None and gopher:
            if event == "UserPromptSubmit" and schedule.report_path(config).is_file():
                emit(event, f"{schedule.NOTIFY_TEXT} (It is waiting from the background schedule; handle it after the user's request.)")
            return 0
        if config is None or not project or not (config.projects_dir / project).is_dir():
            return 0
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
    root = resolve_root(args.root)
    if not (root / ROOT_CONFIG).is_file():
        raise ConfigError(f"{root} is not a gopher root (no {ROOT_CONFIG}); create one with gopher setup")
    return load_config(root)


def checkout_target(config, project: str | None, path: str | None) -> tuple[str, Path]:
    known = project is not None and (config.projects_dir / project).is_dir() and Path(project).name == project
    if project is not None and not known and path is None:
        project, path = None, project
    if project is None:
        project = workon.project_dir_at(config, os.getcwd())
        if project is None:
            raise FileNotFoundError(f"{path or '.'} names no project and {os.getcwd()} is not inside one; usage: gopher checkout add <project> <path>")
    elif not known:
        raise FileNotFoundError(f"no project {project!r} in {config.projects_dir}; usage: gopher checkout add <project> <path>")
    return project, Path(path or ".").expanduser().resolve()


def ask(prompt: str, default: str | None = None) -> str:
    if not sys.stdin.isatty():
        if default:
            return default
        raise RootError(f"{prompt.rstrip(': ')} is required (no terminal to prompt on)")
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default or ""


def need_root_dir(args) -> None:
    if not args.dir and not sys.stdin.isatty():
        raise RootError("give the root directory (no terminal to prompt on), e.g. gopher setup ~/workon")


def cmd_new(args) -> int:
    need_root_dir(args)
    target = Path(args.dir or ask("Root directory", "~/workon")).expanduser().resolve()
    check_target(target)
    registry = load_registry()
    name = args.name or ask("Root name", target.name)
    check_name(name, target, registry)
    make_default = args.default
    if make_default is None and registry.default and registry.default != name:
        make_default = ask(f"Make {name!r} the default root (current: {registry.default})? y/N", "n").lower().startswith("y")
    create_root(target, args.source, choose_schedule(args, target, ask, print))
    register_root(registry, name, target, bool(make_default))
    save_registry(registry)
    print(f"created root {name!r} at {target}" + (" (default)" if registry.default == name else ""))
    return 0


def choose_schedule(args, target: Path, ask, say) -> dict[str, str] | None:
    if args.source or args.default_schedule or not sys.stdin.isatty():
        return None
    return schedule.interview(load_config(target), ask, say)


CONFIRM_WORD = "yes"


def confirm(prompt: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        raise RootError(f"{prompt} needs an answer but there is no terminal; rerun with -y (or the matching --no-* flag)")
    return input(f"{prompt} Type {CONFIRM_WORD} to confirm: ").strip() == CONFIRM_WORD


def cmd_setup(args) -> int:
    if not args.remove_hooks:
        need_root_dir(args)
    return run_setup(args, SetupIO(
        ask=ask, confirm=confirm, say=print, launch=launch_interview,
        schedule=lambda args, target: choose_schedule(args, target, ask, print),
    ))


def workon_target(args, config, ui, switch: bool = False):
    project = workon.resolve_project(config, ui, args.project, os.getcwd())
    return project, workon.resolve_checkout(config, ui, project, args.checkout, os.getcwd(), switch=switch)


def cmd_workon_source(args) -> int:
    config = root_config(args)
    if not os.environ.get(workon.EVAL_ENV):
        raise workon.WorkonError('source changes your shell; load the shell function first: eval "$(gopher shell-init zsh)"')
    project, checkout = workon_target(args, config, workon.TtyUI(), switch=True)
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
        launch.exec_agent(config, args.passthrough, resume=args.resume)
        return 0
    if args.restart:
        for line in launch.restart(config, args.resume):
            print(f"gopher: {line}")
        return 0
    if args.relink:
        session = args.session or launch.current_session()
        if session and (moved := launch.relink(session)):
            print(f"gopher: {moved}")
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
        if args.restart:
            print(workon.restart_sidebar(config, window, project))
        else:
            print(workon.add_sidebar(config, window, project, replace=args.replace))
    return 0


def describe_snapshot(snapshot: restore.WindowSnapshot) -> str:
    roles = ", ".join(
        f"{pane.role}{' (resume ' + pane.claude_session[:8] + ')' if pane.claude_session else ''}" for pane in snapshot.panes
    )
    return f"{len(snapshot.panes)} pane(s): {roles}"


def cmd_workon_open(args) -> int:
    config = root_config(args)
    try:
        window, snapshot = restore.open_window(config, args.project, args.session)
    except restore.RestoreError as error:
        raise RootError(str(error)) from error
    source = "fresh layout" if snapshot.source == "fresh" else "restored snapshot"
    print(f"{args.project}: opened {window} ({source}; {describe_snapshot(snapshot)})")
    return 0


def cmd_workon_close(args) -> int:
    config = root_config(args)
    try:
        snapshot = restore.close_window(config, args.project)
    except restore.RestoreError as error:
        raise RootError(str(error)) from error
    print(f"{args.project}: closed; snapshot {restore.snapshot_path(config, args.project)} ({describe_snapshot(snapshot)})")
    return 0


def cmd_workon_snapshot(args) -> int:
    config = root_config(args)
    try:
        snapshot = restore.capture(config, args.project)
    except restore.RestoreError as error:
        raise RootError(str(error)) from error
    print(f"{args.project}: snapshot {restore.snapshot_path(config, args.project)} ({describe_snapshot(snapshot)})")
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


TMUX_CONF = Path(__file__).resolve().parent / "shell" / "tmux.conf"


def cmd_shell_init(args) -> int:
    if args.path:
        if args.shell != "tmux":
            raise RootError("--path only applies to tmux")
        print(TMUX_CONF)
        return 0
    print(TMUX_CONF.read_text() if args.shell == "tmux" else workon.SHELL_INIT[args.shell], end="")
    return 0


def cmd_commands(args) -> int:
    for name, description in COMMANDS.items():
        print(f"{name}:{description}" if args.describe else name)
    return 0


COMMANDS: dict = {}


def cmd_update(args) -> int:
    config = root_config(args)
    try:
        status = update.check(config)
    except update.UpdateError as error:
        raise RootError(str(error)) from error
    print(status.describe())
    if args.check or not status.available or status.editable:
        return 0
    print("install: " + " ".join(update.install_command(status)))
    if not args.yes:
        if not sys.stdin.isatty():
            print("Nothing changed. Rerun with --yes once the user has approved the update.")
            return 2
        if not confirm(f"Update to v{status.latest}?"):
            print("Nothing changed.")
            return 1
    try:
        update.install(status)
    except update.UpdateError as error:
        raise RootError(str(error)) from error
    print(f"installed v{status.latest}. Then, inside tmux:")
    for line in update.AFTER_UPDATE:
        print(f"  {line}")
    return 0


def cmd_lifecycle(args) -> int:
    config = root_config(args)
    try:
        plan = lifecycle.make_plan(config, args.action, args.project)
    except lifecycle.LifecycleError as error:
        raise RootError(str(error)) from error
    print(lifecycle.describe(plan))
    if plan.blockers:
        return 1
    if args.dry_run:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("Nothing changed. Rerun with --yes once the user has approved this plan.")
            return 2
        if not confirm(f"{args.action.capitalize()} {args.project}?"):
            print("Nothing changed.")
            return 1
    try:
        report = lifecycle.execute(config, plan)
    except lifecycle.LifecycleError as error:
        raise RootError(str(error)) from error
    for line in report:
        print(f"  {line}")
    return 0


def cmd_dev_reload(args) -> int:
    config = root_config(args)
    if not devreload.enabled(config) and not args.force:
        raise RootError("developer reload is off for this root; set [dev] reload = true in config.toml (or pass --force)")
    project = args.project or os.environ.get("GOPHER_PROJECT")
    print("smoke-starting the TUI..." if not args.skip_smoke else "skipping the smoke test")
    try:
        signal = devreload.publish(config, project, smoke=not args.skip_smoke)
    except devreload.DevReloadError as error:
        raise RootError(str(error)) from error
    print(f"published {signal['token']} ({signal['sha'] or 'no git'}); sidebars with [dev] reload on will restart")
    return 0


def cmd_save(args) -> int:
    config = root_config(args)
    try:
        report = rootgit.save(config, push=not args.no_push, dry_run=args.dry_run)
    except rootgit.SaveError as error:
        raise RootError(str(error)) from error
    if not report.commits and not report.pushed:
        print("nothing to save")
        return 0
    verb = "would commit" if args.dry_run else "committed"
    for message in report.commits:
        print(f"{verb}: {message}")
    if report.pushed:
        print("pushed")
    if report.local_only and report.commits and not args.dry_run:
        print("not pushed: the root has no git remote (gopher doctor shows how to add one)")
    return 0


def cmd_schedule_run(args) -> int:
    try:
        schedule.run_loop(root_config(args), schedule.parse_duration(args.interval))
    except schedule.ScheduleError as error:
        raise RootError(str(error)) from error
    return 0


def cmd_schedule_report(args) -> int:
    text = schedule.take_report(root_config(args))
    print(text.rstrip() if text else "nothing pending")
    return 0


def cmd_schedule_tick(args) -> int:
    report = schedule.tick(root_config(args), dry_run=args.dry_run)
    if report.sections:
        print(report.text())
    return 0


def when(timestamp: float | None, now: float) -> str:
    if timestamp is None:
        return "-"
    if timestamp <= now:
        return "now" if now - timestamp < 60 else f"{schedule.format_age(now - timestamp)} ago"
    return f"in {schedule.format_seconds(timestamp - now)}"


def cmd_schedule_add_default(args) -> int:
    written = schedule.add_defaults(root_config(args), args.names)
    for path in written:
        print(f"added {path}")
    if not written:
        print("no built-in tasks missing")
    return 0


def cmd_schedule_list(args) -> int:
    config = root_config(args)
    now = time.time()
    rows, problems = schedule.listing(config, now)
    if not rows and not problems:
        print(f"no tasks in {config.schedule_dir}")
    if rows:
        table = [(row, when(row.last, now) if row.last else "never", when(row.next, now)) for row in rows]
        width = max(len(row.name) for row in rows)
        timing = max(len(row.timing) for row in rows)
        lasts = max(4, *(len(last) for _, last, _ in table))
        nexts = max(4, *(len(upcoming) for _, _, upcoming in table))
        tells = max(len("GOPHER"), *(len(row.tells) for row in rows))
        print(f"{'TASK':<{width}}  {'WHEN':<{timing}}  {'LAST':<{lasts}}  {'NEXT':<{nexts}}  {'GOPHER':<{tells}}  RUN")
        for row, last, upcoming in table:
            line = f"{row.name:<{width}}  {row.timing:<{timing}}  {last:<{lasts}}  {upcoming:<{nexts}}  {row.tells:<{tells}}  {row.run or '-'}"
            print(line + (f"  ({row.note})" if row.note else ""))
    for name, problem in problems.items():
        print(f"{name}: {problem}", file=sys.stderr)
    if (summary := schedule.heartbeat_summary(config, now)):
        print(summary[0])
    return 1 if problems else 0


def cmd_project_new(args) -> int:
    config = root_config(args)
    project_dir = create_project(config.projects_dir, args.name, args.summary)
    for checkout in args.checkout:
        add_checkout(config.projects_dir, args.name, Path(checkout).expanduser().resolve())
    print(project_dir)
    return 0


def parse_priority(value: str | None, current: int) -> int:
    if value in (None, ""):
        return current
    if value in ("+", "up"):
        return current + 1
    if value in ("-", "down"):
        return current - 1
    try:
        return int(value)
    except ValueError:
        raise RootError(f"priority must be 0-{MAX_PRIORITY}, + or -, not {value!r}") from None


def cmd_project_priority(args) -> int:
    config = root_config(args)
    project_dir = config.projects_dir / args.name
    if not project_dir.is_dir():
        raise RootError(f"no project named {args.name}")
    current = read_priority(project_dir)
    priority = parse_priority(args.value, current)
    if priority != current:
        priority = write_priority(project_dir, priority)
    print(f"{args.name}: priority {priority} ({'*' * priority or 'none'})")
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
    args.project, path = checkout_target(config, args.project, args.path)
    add_checkout(config.projects_dir, args.project, path, args.shared)
    others = [o for o in owners(config.projects_dir, path) if o != args.project]
    if others and path not in all_shared(config.projects_dir):
        print(f"note: {path} is also owned by {', '.join(others)}; add --shared if it is shared on purpose", file=sys.stderr)
    return 0


def cmd_checkout_remove(args) -> int:
    config = root_config(args)
    args.project, path = checkout_target(config, args.project, args.path)
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
    shared = all_shared(config.projects_dir)
    rows = [(name, str(path), "shared" if path in shared else "") for name, path in all_checkouts(config.projects_dir)]
    print_table(("PROJECT", "CHECKOUT", "SHARED"), rows)
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


def check_line(check) -> str:
    return f"{check.level:<4}  {check.area:<9}  {check.message}"


def cmd_clean(args) -> int:
    now = time.time()
    large = int(args.min_size * 1024 * 1024)
    result = clean.report(root_config(args), now, args.days, large)
    print("\n".join(clean.render(result, now, args.days, large)))
    return 0


def cmd_prs_sync(args) -> int:
    try:
        changes, errors = prsync.sync(root_config(args), dry_run=args.dry_run)
    except prsync.SyncError as error:
        print(f"gopher prs sync: {error}", file=sys.stderr)
        return 1
    for change in changes:
        print(f"{change.project}: {change.describe()}" + (" (dry run)" if args.dry_run else ""))
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def cmd_doctor(args) -> int:
    checks = doctor.run(root_config(args))
    if args.report:
        return doctor_report(args, checks)
    for check in checks:
        if args.quiet and check.level == doctor.OK:
            continue
        print(check_line(check))
    failed = sum(check.level == doctor.FAIL for check in checks)
    warned = sum(check.level == doctor.WARN for check in checks)
    if not args.quiet or failed or warned:
        print(f"{failed} problem(s), {warned} warning(s)" if failed or warned else "all good")
    return 1 if failed else 0


def doctor_report(args, checks) -> int:
    title = args.report.strip()
    body = bugreport.draft(title, args.description or "", [check_line(check) for check in checks], bugreport.environment())
    if args.comment is None:
        try:
            matches = bugreport.search(title)
        except bugreport.BugReportError as error:
            print(f"could not search existing issues: {error}", file=sys.stderr)
            matches = []
    else:
        matches = []
    print(f"Title: {title}\n\n{body}")
    if matches:
        print("Possible duplicates:")
        for match in matches:
            print(f"  #{match.number} [{match.state}] {match.title}  {match.url}")
        print()
    if args.comment is not None:
        print(bugreport.comment(args.comment, body))
        return 0
    if args.yes:
        print(bugreport.file_issue(title, body))
        return 0
    if not sys.stdin.isatty():
        print(f"Nothing posted. This goes to the public issue tracker at {bugreport.REPO}: re-run with --yes to file it,"
              " or --comment N to add it to an existing issue.")
        return 0
    answer = input("File a new issue [n], add to an existing one [its number], or quit [q]? ").strip().lower().lstrip("#")
    if answer == "n":
        print(bugreport.file_issue(title, body))
    elif answer.isdigit():
        print(bugreport.comment(int(answer), body))
    else:
        print("Nothing posted.")
    return 0


def cmd_needs_login(args) -> int:
    config = root_config(args)
    pane = args.pane or os.environ.get("TMUX_PANE")
    if not pane:
        raise RootError("gopher needs-login identifies the agent by its tmux pane; run it inside tmux or pass --pane")
    marked = mark_needs_login(config.sessions_dir, pane, not args.clear)
    if not marked:
        raise RootError(f"no agent session recorded for pane {pane}; are gopher's hooks installed?")
    print(("cleared" if args.clear else "marked as needing login") + f": {', '.join(marked)}")
    return 0


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def cmd_projects(args) -> int:
    config = root_config(args)
    if args.names:
        print("\n".join(directory.name for directory in project_dirs(config.projects_dir)))
        return 0
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gopher")
    parser.add_argument("--root", help="root name or path (default: $GOPHER_HOME, then the registry default)")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a gopher root")
    new.add_argument("dir", nargs="?", help="root directory (prompted if omitted)")
    new.add_argument("--name", help="registry name (default: directory basename)")
    new.add_argument("--from", dest="source", metavar="GIT_URL", help="clone an existing root")
    new.add_argument("--default-schedule", action="store_true", help="use the default schedule without asking")
    default = new.add_mutually_exclusive_group()
    default.add_argument("--default", dest="default", action="store_true", default=None)
    default.add_argument("--no-default", dest="default", action="store_false")
    new.set_defaults(func=cmd_new)

    setup = sub.add_parser("setup", help="create or set up a gopher root on this machine, then interview to configure it")
    setup.add_argument("dir", nargs="?", help="root directory (prompted if omitted)")
    setup.add_argument("--name", help="registry name (default: directory basename)")
    setup.add_argument("--from", dest="source", metavar="GIT_URL", help="clone an existing root")
    setup.add_argument("--default-schedule", action="store_true", help="use the default schedule without asking")
    setup_default = setup.add_mutually_exclusive_group()
    setup_default.add_argument("--default", dest="default", action="store_true", default=None)
    setup_default.add_argument("--no-default", dest="default", action="store_false")
    setup.add_argument("--no-hooks", action="store_true", help="do not offer to install Claude Code hooks")
    setup.add_argument("--no-interview", action="store_true", help="do not offer the setup interview")
    setup.add_argument("--review", action="store_true", help="only run the interview, revising an existing root")
    setup.add_argument("--remove-hooks", action="store_true", help="remove gopher's Claude Code hooks from ~/.claude/settings.json (roots are left alone)")
    setup.add_argument("-y", "--yes", action="store_true", help="install hooks without asking")
    setup.set_defaults(func=cmd_setup)

    launcher = sub.add_parser("launch", help="start the Gopher agent's tmux session and link its window at index 0")
    launcher.add_argument("--session", help="tmux session to link into (default: the current one)")
    launcher.add_argument("--relink", action="store_true", help="only move the Gopher window back to index 0 if it drifted (for a window-unlinked hook)")
    launcher.add_argument("--restart", action="store_true", help="restart the Gopher agent (picks up a new gopher version and prompt); its window and links stay")
    launcher.add_argument("--continue", dest="resume", action="store_true", help="with --restart: continue Gopher's last conversation instead of starting fresh")
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

    for action, summary in (
        ("archive", "park a project: move it to the archive and run archive cleanup in its checkouts"),
        ("close", "end a project: run close cleanup, remove its git worktrees, move it to closed"),
        ("reopen", "move an archived or closed project back to the open projects"),
    ):
        lifecycle_parser = sub.add_parser(action, help=summary)
        lifecycle_parser.add_argument("project")
        lifecycle_parser.add_argument("--yes", action="store_true", help="skip the typed confirmation (only after the user approved the plan)")
        lifecycle_parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
        lifecycle_parser.set_defaults(func=cmd_lifecycle, action=action)

    dev = sub.add_parser("dev", help="developer tools (opt-in with [dev] in config.toml)").add_subparsers(dest="dev_command", required=True)
    dev_reload = dev.add_parser("reload", help="smoke-test the TUI, then tell every sidebar to restart onto the current code")
    dev_reload.add_argument("--project", help="project to pin in the smoke test (default: $GOPHER_PROJECT)")
    dev_reload.add_argument("--skip-smoke", action="store_true", help="publish without the headless start check")
    dev_reload.add_argument("--force", action="store_true", help="publish even if [dev] reload is off")
    dev_reload.set_defaults(func=cmd_dev_reload)

    updater = sub.add_parser("update", help="check for a newer gopher release and install it")
    updater.add_argument("--check", action="store_true", help="only report whether a newer release exists")
    updater.add_argument("-y", "--yes", action="store_true", help="install without asking (agents: only after the user agrees)")
    updater.set_defaults(func=cmd_update)

    saver = sub.add_parser("save", help="commit the root's changes (one commit per project, one for root files) and push")
    saver.add_argument("--no-push", action="store_true", help="commit only")
    saver.add_argument("--dry-run", action="store_true", help="show the commits without making them")
    saver.set_defaults(func=cmd_save)

    scheduler = sub.add_parser("schedule", help="Gopher's recurring tasks (the root's schedule/ directory); tick runs what is due")
    scheduler.set_defaults(func=cmd_schedule_list)
    schedule_sub = scheduler.add_subparsers(dest="schedule_command")
    schedule_tick = schedule_sub.add_parser("tick", help="run the tasks that are due and print what Gopher should do (its heartbeat)")
    schedule_tick.add_argument("--dry-run", action="store_true", help="show what is due without running or recording it")
    schedule_tick.set_defaults(func=cmd_schedule_tick)
    schedule_sub.add_parser("list", help="list tasks with their last and next runs").set_defaults(func=cmd_schedule_list)
    schedule_run = schedule_sub.add_parser("run", help="the background loop gopher launch starts: tick, then tell Gopher only when there is output")
    schedule_run.add_argument("--interval", default="10m", help="time between ticks (default 10m)")
    schedule_run.set_defaults(func=cmd_schedule_run)
    schedule_add = schedule_sub.add_parser("add-default", help="write built-in tasks missing from schedule/ (all of them, or the ones named)")
    schedule_add.add_argument("names", nargs="*", metavar="NAME")
    schedule_add.set_defaults(func=cmd_schedule_add_default)
    schedule_sub.add_parser("report", help="print and clear the schedule output waiting for Gopher").set_defaults(func=cmd_schedule_report)

    project = sub.add_parser("project", help="manage projects").add_subparsers(dest="project_command", required=True)
    project_new = project.add_parser("new", help="create a project")
    project_new.add_argument("name")
    project_new.add_argument("--summary", default="")
    project_new.add_argument("--checkout", action="append", default=[], help="checkout path owned by the project")
    project_new.set_defaults(func=cmd_project_new)
    project_priority = project.add_parser("priority", help=f"show or set a project's priority (0-{MAX_PRIORITY} stars, in human/STATUS.md)")
    project_priority.add_argument("name")
    project_priority.add_argument("value", nargs="?", help=f"0-{MAX_PRIORITY}, + or -")
    project_priority.set_defaults(func=cmd_project_priority)
    project_rename = project.add_parser("rename", help="rename a project: directory, STATUS heading, gopher state, tmux windows")
    project_rename.add_argument("old")
    project_rename.add_argument("new")
    project_rename.add_argument("--no-windows", action="store_true", help="leave tmux window names alone")
    project_rename.set_defaults(func=cmd_project_rename)

    checkout = sub.add_parser("checkout", help="manage project checkouts").add_subparsers(dest="checkout_command", required=True)
    checkout_add = checkout.add_parser("add", help="give a project a checkout: add <project> <path>, or add <path> inside a project dir")
    checkout_add.add_argument("project", nargs="?")
    checkout_add.add_argument("path", nargs="?")
    checkout_add.add_argument("--shared", action=argparse.BooleanOptionalAction, default=None,
                              help="a long-lived checkout used across projects: close and archive leave it alone, and several projects may list it")
    checkout_add.set_defaults(func=cmd_checkout_add)
    checkout_remove = checkout.add_parser("remove", help="drop a checkout from a project: remove <project> <path>")
    checkout_remove.add_argument("project", nargs="?")
    checkout_remove.add_argument("path", nargs="?")
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
    sidebar.add_argument("--restart", action="store_true", help="restart running sidebars on the installed gopher (after an update)")
    sidebar.set_defaults(func=cmd_workon_sidebar)
    open_window = workon_sub.add_parser("open", help=workon.ACTIONS["open"])
    open_window.add_argument("project")
    open_window.add_argument("--session", help="tmux session for the window (default: the snapshot's, else the current one)")
    open_window.set_defaults(func=cmd_workon_open)
    for action, handler in (("close", cmd_workon_close), ("snapshot", cmd_workon_snapshot)):
        window_action = workon_sub.add_parser(action, help=workon.ACTIONS[action])
        window_action.add_argument("project")
        window_action.set_defaults(func=handler)
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
    shell_init.add_argument("shell", choices=sorted([*workon.SHELL_INIT, "tmux"]), help="bash or zsh: shell function and completion; tmux: hooks and the prefix g popup")
    shell_init.add_argument("--path", action="store_true", help="tmux only: print the installed tmux.conf's path, to source-file it")
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

    doctor_parser = sub.add_parser("doctor", help="check the install: hooks, tmux config, Gopher, backup, schedule and heartbeat, checkouts")
    doctor_parser.add_argument("-q", "--quiet", action="store_true", help="print only warnings and problems")
    doctor_parser.add_argument("--report", metavar="TITLE", help="draft a bug report with these results and search existing issues; posts only with --yes or --comment")
    doctor_parser.add_argument("--description", default="", help="what happened, for --report")
    doctor_parser.add_argument("--yes", action="store_true", help="with --report: file the issue without asking")
    doctor_parser.add_argument("--comment", type=int, metavar="N", help="with --report: add the report to existing issue N instead")
    doctor_parser.set_defaults(func=cmd_doctor)

    prs = sub.add_parser("prs", help="pull requests in project resources").add_subparsers(dest="prs_command", required=True)
    prs_sync = prs.add_parser("sync", help="ask gh for each open or draft PR in resources/prs/, update its state, and file an inbox item per changed project")
    prs_sync.add_argument("--dry-run", action="store_true", help="print the changes without writing anything")
    prs_sync.set_defaults(func=cmd_prs_sync)

    clean_parser = sub.add_parser("clean", help="suggest cleanups: stale projects, worktrees no project owns, large temp folders (changes nothing)")
    clean_parser.add_argument("--days", type=int, default=clean.STALE_WORKDAYS, help="working days without a STATUS.md update before a project counts as stale")
    clean_parser.add_argument("--min-size", type=float, default=clean.LARGE_BYTES / 1024 / 1024, metavar="MB", help="temp folders at least this big are listed")
    clean_parser.set_defaults(func=cmd_clean)

    needs_login = sub.add_parser("needs-login", help="mark this agent as blocked on credentials (shows 'needs login'; cleared by the user's next message)")
    needs_login.add_argument("--clear", action="store_true", help="clear the mark now")
    needs_login.add_argument("--pane", help="the agent's tmux pane (default: $TMUX_PANE)")
    needs_login.set_defaults(func=cmd_needs_login)


    projects = sub.add_parser("projects", help="project overview: state, agents, inbox, checkouts, summary")
    projects.add_argument("--json", action="store_true")
    projects.add_argument("--names", action="store_true", help="project names only, one per line")
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
    except (ConfigError, RootError, FileNotFoundError, workon.WorkonError, bugreport.BugReportError, schedule.ScheduleError) as error:
        print(f"gopher: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
