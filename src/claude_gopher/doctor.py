import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from claude_gopher import rootgit, schedule, update
from claude_gopher.checkouts import all_checkouts, all_shared
from claude_gopher.config import Config, ConfigError, gopher_proactivity, gopher_save_every_hours, gopher_update_check, tui_actions
from claude_gopher.hooks import HOOKS, gopher_command, has_hook, is_gopher_hook, read_settings, settings_path

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    level: str
    area: str
    message: str


def tmux(*argv: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *argv], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(["tmux", *argv], 1, "", str(error))


def check_config(config: Config) -> list[Check]:
    problems = []
    for read in (gopher_proactivity, gopher_save_every_hours, gopher_update_check, tui_actions):
        try:
            read(config)
        except ConfigError as error:
            problems.append(Check(FAIL, "config", str(error)))
    if not config.state_dir.is_dir():
        problems.append(Check(FAIL, "config", f"state dir {config.state_dir} is missing; run gopher setup {config.root}"))
    return problems or [Check(OK, "config", f"{config.root / 'config.toml'} loads")]


def check_backup(config: Config, now: float) -> list[Check]:
    status = rootgit.backup_status(config.root)
    every = schedule.save_interval_hours(config)
    level = {"ok": OK, "overdue": WARN, "error": FAIL}[status.level(now, every)]
    return [Check(level, "backup", status.describe(now, every).removeprefix("backup: "))]


def check_hooks(path: Path) -> list[Check]:
    settings = read_settings(path)
    events = settings.get("hooks", {})
    missing = [
        f"{event}{f' [{matcher}]' if matcher else ''} -> hook {name}"
        for name, wanted in HOOKS.items() for event, matcher in wanted.items()
        if not has_hook(events.get(event, []), name, matcher)
    ]
    commands = {
        hook.get("command", "") for entries in events.values() for entry in entries for hook in entry.get("hooks", [])
        if any(is_gopher_hook(hook.get("command", ""), name) for name in HOOKS)
    }
    stale = sorted({command.split()[-3] for command in commands if not shutil.which(command.split()[-3])})
    checks = []
    if missing:
        checks.append(Check(FAIL, "hooks", f"missing in {path}: {'; '.join(missing)}. Run gopher setup to add them"))
    if stale:
        checks.append(Check(FAIL, "hooks", f"hook commands point at a missing gopher: {', '.join(stale)} (now {gopher_command()}); fix them in {path}"))
    return checks or [Check(OK, "hooks", f"all gopher hooks installed in {path}")]


def check_tmux(env: dict) -> list[Check]:
    if not shutil.which("tmux"):
        return [Check(FAIL, "tmux", "tmux is not installed")]
    if not env.get("TMUX"):
        return [Check(WARN, "tmux", "not running inside tmux; sidebars, windows, and popups need it")]
    checks = []
    binding = tmux("list-keys", "-T", "prefix", "g").stdout
    if "gopher" not in binding:
        checks.append(Check(WARN, "tmux", "prefix g is not gopher's popup; add the line from `gopher shell-init tmux --path` to your tmux config"))
    if tmux("has-session", "-t", "=gopher").returncode != 0:
        checks.append(Check(WARN, "gopher", "the Gopher session is not running; start it with gopher launch"))
    elif "schedule" not in tmux("list-windows", "-t", "=gopher", "-F", "#{window_name}").stdout.split():
        checks.append(Check(WARN, "schedule", "the background schedule loop is not running; gopher launch starts it"))
    return checks or [Check(OK, "tmux", "gopher's tmux config is loaded and Gopher is running")]


def check_schedule(config: Config, now: float) -> list[Check]:
    if not config.schedule_dir.is_dir():
        return [Check(WARN, "schedule", f"no {config.schedule_dir}; Gopher's first heartbeat creates it")]
    rows, problems = schedule.listing(config, now)
    checks = [Check(FAIL, "schedule", f"schedule/{name}.md: {problem}") for name, problem in problems.items()]
    checks += [Check(WARN, "schedule", f"{row.name} last exited {row.note.split()[-1]}") for row in rows if "last exit" in row.note]
    last = schedule.load_state(config).get("last_tick")
    if not last:
        checks.append(Check(WARN, "heartbeat", "no tick yet; gopher launch starts the background schedule loop"))
    elif now - last > schedule.HEARTBEAT_STALE_SECONDS:
        checks.append(Check(WARN, "heartbeat", f"last tick {schedule.format_age(now - last)} ago; restart the loop with gopher launch --restart --continue"))
    return checks or [Check(OK, "schedule", f"{len(rows)} task(s); heartbeat {schedule.format_age(now - last)} ago")]


def check_checkouts(config: Config) -> list[Check]:
    owned = all_checkouts(config.projects_dir)
    by_path: dict[Path, list[str]] = {}
    for project, path in owned:
        by_path.setdefault(path, []).append(project)
    checks = [Check(WARN, "checkouts", f"{path} (owned by {', '.join(projects)}) does not exist") for path, projects in by_path.items() if not path.exists()]
    shared = all_shared(config.projects_dir)
    checks += [
        Check(WARN, "checkouts", f"{path} is owned by {', '.join(projects)}; keep one with gopher checkout move, or mark it gopher checkout add --shared")
        for path, projects in by_path.items() if len(projects) > 1 and path not in shared
    ]
    return checks or [Check(OK, "checkouts", f"{len(by_path)} checkout(s), each with one owner or shared")]


def check_install() -> list[Check]:
    try:
        version = update.installed_version()
    except Exception as error:
        return [Check(WARN, "install", f"cannot read the installed version: {error}")]
    editable = update.editable_dir()
    return [Check(OK, "install", f"claude-gopher {version}" + (f" (editable from {editable})" if editable else ""))]


def run(config: Config, env: dict | None = None, now: float | None = None, settings: Path | None = None) -> list[Check]:
    env = dict(os.environ) if env is None else env
    now = now or time.time()
    return [
        *check_install(), *check_config(config), *check_hooks(settings or settings_path()), *check_tmux(env),
        *check_backup(config, now), *check_schedule(config, now), *check_checkouts(config),
    ]
