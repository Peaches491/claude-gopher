import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from claude_gopher import rootgit, schedule, update
from claude_gopher.checkouts import all_checkouts, all_shared
from claude_gopher.config import Config, ConfigError, gopher_proactivity, gopher_save_every_hours, gopher_update_check, tui_actions
from claude_gopher.hooks import HOOKS, gopher_command, has_hook, is_gopher_hook, read_settings, settings_path
from claude_gopher.overview import current_sessions

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    level: str
    area: str
    message: str

    @property
    def key(self) -> str:
        return f"{self.area}: {self.message}"


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
    level = {"ok": OK, "overdue": WARN, "local": WARN, "error": FAIL}[status.level(now, every)]
    message = status.describe(now, every).removeprefix("backup: ")
    if status.local:
        root = shlex.quote(str(config.root))
        message += f"; add a private remote: git -C {root} remote add origin <url> && git -C {root} push -u origin HEAD"
    return [Check(level, "backup", message)]


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
    entries = schedule.load_state(config).get("tasks", {})
    for row in rows:
        entry = entries.get(row.name, {})
        if entry.get("exit"):
            lines = [line for line in (entry.get("output") or "").splitlines() if line.strip()]
            checks.append(Check(WARN, "schedule", f"{row.name} failed (exit {entry['exit']})" + (f": {lines[-1].strip()}" if lines else "")))
    for task in schedule.missing_defaults(config):
        checks.append(Check(WARN, "schedule", f"built-in task {task.name} ({task.purpose}) is not in schedule/; add it with gopher schedule add-default {task.name}"))
    last = schedule.load_state(config).get("last_tick")
    if not last:
        checks.append(Check(WARN, "heartbeat", "no tick yet; gopher launch starts the background schedule loop"))
    elif now - last > schedule.HEARTBEAT_STALE_SECONDS:
        checks.append(Check(WARN, "heartbeat", f"last tick {schedule.format_age(now - last)} ago; restart the loop with gopher launch --restart --continue"))
    return checks or [Check(OK, "schedule", f"{len(rows)} task(s); heartbeat {schedule.format_age(now - last)} ago")]


def check_update(config: Config, now: float) -> list[Check]:
    entry = schedule.load_state(config).get("tasks", {}).get("update-check", {})
    if not entry.get("last") or entry.get("exit"):
        return []
    checked = f"checked {schedule.format_age(now - entry['last'])} ago"
    try:
        installed = update.installed_version()
    except Exception:
        return []
    latest = update.reported_latest(entry.get("output") or "")
    if latest and update.version_key(latest) > update.version_key(installed):
        how = "git pull in your checkout" if update.editable_dir() else "gopher update"
        return [Check(WARN, "update", f"v{latest} is available (installed {installed}); {how}")]
    return [Check(OK, "update", f"{installed} is current ({checked})")]


def check_gopher_login(config: Config) -> list[Check]:
    pane = schedule.gopher_pane()
    if not pane:
        return []
    if any(s.get("pane") == pane and s.get("blocked_creds") for s in current_sessions(config, prune=False)):
        return [Check(WARN, "gopher", "Gopher needs a login renewed; see its pane")]
    return []


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
        *check_install(), *check_update(config, now), *check_config(config), *check_hooks(settings or settings_path()), *check_tmux(env),
        *check_gopher_login(config), *check_backup(config, now), *check_schedule(config, now), *check_checkouts(config),
    ]


def problems(checks: list[Check]) -> list[Check]:
    return [check for check in checks if check.level != OK]


def dismissed_path(config: Config) -> Path:
    return config.state_dir / "doctor-dismissed.json"


def load_dismissed(config: Config) -> set[str]:
    try:
        return set(json.loads(dismissed_path(config).read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def save_dismissed(config: Config, keys: set[str]) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    dismissed_path(config).write_text(json.dumps(sorted(keys)))


def undismissed(config: Config, checks: list[Check]) -> list[Check]:
    current = {check.key for check in problems(checks)}
    dismissed = load_dismissed(config)
    if dismissed - current:
        save_dismissed(config, dismissed & current)
    return [check for check in problems(checks) if check.key not in dismissed]


def dismiss(config: Config, checks: list[Check]) -> int:
    keys = {check.key for check in problems(checks)}
    save_dismissed(config, load_dismissed(config) | keys)
    return len(keys)
