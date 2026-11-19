import datetime
import fcntl
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_gopher import rootgit
from claude_gopher.overview import current_sessions
from claude_gopher.config import Config, ConfigError, gopher_proactivity, gopher_save_every_hours, gopher_update_check
from claude_gopher.projects import read_frontmatter

README = "README.md"
DURATION = re.compile(r"^(\d+)\s*([mhd])$")
UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SETS = {"daily": DAY_NAMES, "weekdays": DAY_NAMES[:5], "weekends": DAY_NAMES[5:]}
KNOWN_FIELDS = {"every", "at", "days", "run", "until", "enabled", "notify"}
NOTIFY_MODES = ("always", "failure", "change")
RUN_TIMEOUT = 600
OUTPUT_LINES = 40
STALE_WEEKDAYS = 5
HEARTBEAT_STALE_SECONDS = 30 * 60
LOOP_SECONDS = 10 * 60
DELIVERY_CHECK_SECONDS = 30
RENOTIFY_SECONDS = 30 * 60
REPORT_FILE = "schedule-report.md"
NOTIFY_TEXT = "Schedule: run `gopher schedule report` and act on what it prints."
PROMPT_MARK = "\u276f"
OPEN_STATES = ("active", "blocked", "waiting-review")

SCHEDULE_README = """# Schedule

Gopher's recurring work. One file per task: frontmatter says when, the body says what Gopher does
with the result. A background loop (`gopher schedule run`, started by `gopher launch`) ticks
every 10 minutes: it runs the tasks that are due and hands Gopher their output and instructions,
and stays silent when there is nothing to say. `gopher schedule` lists
the tasks with their last and next runs. Edit these files yourself or ask Gopher to.

Frontmatter fields:

- `every: 10m` / `2h` / `1d`: run at this interval. Or
- `at: "08:45"`: run once a day at this local time (after it, if the loop was not running).
- `days: weekdays`: limit to `daily` (default), `weekdays`, `weekends`, or a list like `mon,wed,fri`.
- `run: <command>`: a shell command the tick runs in the root first; its output goes to Gopher.
- `until: YYYY-MM-DD`: last day to run; afterwards the tick reports it once so Gopher can remove it.
- `notify: failure` / `notify: change`: tell Gopher only when `run` fails, or when its output
  differs from the previous run (default `always`).
- `enabled: false`: keep the file but skip it.

The body is plain instructions for Gopher. A task without `run` is just a reminder: Gopher does
what the body says (read a thread, check a dashboard) and reports at most a line.
"""


SAVE_BODY = (
    "The save failed: tell the user in one line what it said (a merge in progress, a rejected\n"
    "push) and do not try to fix the repository yourself."
)
UPDATE_BODY = (
    "If it reports a newer release, do not interrupt the user: note it in the journal and mention it\n"
    "in one line at the next briefing or the next time they talk to you, with the release notes link.\n"
    "Mention a given version once. Install only when the user says so."
)
DEFAULT_UPDATE_AT = "00:03"
BUILTIN_TASKS = ("save", "update-check")
BUILTIN_NOTIFY = {"save": "failure", "update-check": "change"}


@dataclass
class Default:
    name: str
    when: str
    run: str
    body: str
    purpose: str
    notify: str = "always"


def timing_lines(when: str) -> list[str]:
    every = at = days = None
    for token in when.replace(" at ", " ").split():
        token = token.strip().lower()
        if token == "at":
            continue
        if DURATION.match(token):
            every = token
        elif CLOCK.match(token):
            at = token
        else:
            days = token
    if (every is None) == (at is None):
        raise ScheduleError(f"say when as an interval (30m, 2h, 1d) or a time (09:00), optionally with days, not {when!r}")
    if every:
        parse_duration(every)
    lines = [f"every: {every}"] if every else [f'at: "{at}"']
    if days:
        parse_days(days)
        lines.append(f"days: {days}")
    return lines


def task_text(when: str, body: str, run: str | None = None, notify: str = "always") -> str:
    extra = ([f"run: {run}"] if run else []) + ([f"notify: {notify}"] if notify != "always" else [])
    lines = ["---", *timing_lines(when), *extra, "---", body.strip(), ""]
    return "\n".join(lines)


def defaults(config: Config) -> list[Default]:
    try:
        hours = gopher_save_every_hours(config)
        check_updates = gopher_update_check(config)
    except ConfigError:
        hours, check_updates = 0, False
    tasks = []
    if hours:
        tasks.append(Default("save", f"{hours}h", "gopher save", SAVE_BODY, "commit and push the root (gopher save)", BUILTIN_NOTIFY["save"]))
    if check_updates:
        tasks.append(Default("update-check", DEFAULT_UPDATE_AT, "gopher update --check", UPDATE_BODY, "check for a new gopher release", BUILTIN_NOTIFY["update-check"]))
    return tasks


def default_files(config: Config) -> dict[str, str]:
    return {task.name: task_text(task.when, task.body, task.run, task.notify) for task in defaults(config)}


def interview(config: Config, ask, say) -> dict[str, str]:
    say("Gopher's schedule: recurring work run in the background, one file per task in schedule/.")
    files = {}
    for task in defaults(config):
        say(f"  {task.name}: {task.purpose}, {'every ' if DURATION.match(task.when) else 'daily at '}{task.when}")
    for task in defaults(config):
        while True:
            answer = ask(f"Keep {task.name}? y, n, or a new time ({'e.g. 4h' if DURATION.match(task.when) else 'e.g. 06:30'})", "y").strip()
            if answer.lower() in ("n", "no"):
                break
            when = task.when if answer.lower() in ("y", "yes", "") else answer
            try:
                files[task.name] = task_text(when, task.body, task.run, task.notify)
                break
            except ScheduleError as error:
                say(str(error))
    while True:
        name = ask("Add a task? Give it a short name, or press enter to finish", "").strip()
        if not name:
            break
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or name in files or name == README.removesuffix(".md"):
            say("use a new lowercase name with letters, digits and dashes")
            continue
        while True:
            when = ask(f"When should {name} run? (30m, 2h, 1d, 09:00, weekdays 09:00)", "").strip()
            try:
                timing_lines(when)
                break
            except ScheduleError as error:
                say(str(error))
        body = ask(f"What should Gopher do for {name}?", "").strip() or "Remind the user."
        files[name] = task_text(when, body)
    say(f"Schedule: {', '.join(files) if files else 'empty'}. Edit schedule/ any time, or ask Gopher.")
    return files


class ScheduleError(Exception):
    pass


@dataclass
class Task:
    name: str
    path: Path
    every: int | None = None
    at: tuple[int, int] | None = None
    days: tuple[str, ...] = DAY_NAMES
    run: str | None = None
    until: datetime.date | None = None
    enabled: bool = True
    notify: str = "always"
    body: str = ""

    def timing(self) -> str:
        when = f"every {format_seconds(self.every)}" if self.every else f"at {self.at[0]:02d}:{self.at[1]:02d}"
        if self.days != DAY_NAMES:
            when += " " + next((name for name, days in DAY_SETS.items() if days == self.days), ",".join(self.days))
        return when


def parse_duration(value: str) -> int:
    match = DURATION.match(value.strip())
    if not match or int(match.group(1)) == 0:
        raise ScheduleError(f"every must look like 10m, 2h or 1d, not {value!r}")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2)]


def parse_days(value: str) -> tuple[str, ...]:
    value = value.strip().lower()
    if value in DAY_SETS:
        return DAY_SETS[value]
    days = tuple(part.strip()[:3] for part in value.split(",") if part.strip())
    if not days or any(day not in DAY_NAMES for day in days):
        raise ScheduleError(f"days must be daily, weekdays, weekends or a list like mon,wed,fri, not {value!r}")
    return days


def parse_task(path: Path) -> Task:
    fields, body = read_frontmatter(path.read_text())
    task = Task(name=path.stem, path=path, body=body.strip())
    unknown = set(fields) - KNOWN_FIELDS
    if unknown:
        raise ScheduleError(f"unknown field {', '.join(sorted(unknown))}")
    if ("every" in fields) == ("at" in fields):
        raise ScheduleError("needs exactly one of every or at")
    if "every" in fields:
        task.every = parse_duration(fields["every"])
    else:
        match = CLOCK.match(fields["at"])
        if not match:
            raise ScheduleError(f"at must be a local time like 08:45, not {fields['at']!r}")
        task.at = (int(match.group(1)), int(match.group(2)))
    if "days" in fields:
        task.days = parse_days(fields["days"])
    task.run = fields.get("run") or None
    if fields.get("until"):
        try:
            task.until = datetime.date.fromisoformat(fields["until"])
        except ValueError as error:
            raise ScheduleError(f"until must be a date like 2026-12-01, not {fields['until']!r}") from error
    if "enabled" in fields:
        if fields["enabled"].lower() not in ("true", "false"):
            raise ScheduleError(f"enabled must be true or false, not {fields['enabled']!r}")
        task.enabled = fields["enabled"].lower() == "true"
    if "notify" in fields:
        if fields["notify"] not in NOTIFY_MODES:
            raise ScheduleError(f"notify must be one of {', '.join(NOTIFY_MODES)}, not {fields['notify']!r}")
        task.notify = fields["notify"]
    elif task.name in BUILTIN_NOTIFY:
        task.notify = BUILTIN_NOTIFY[task.name]
    return task


def schedule_dir(config: Config) -> Path:
    return config.schedule_dir


def seed(config: Config, files: dict[str, str] | None = None) -> list[Path]:
    directory = schedule_dir(config)
    if directory.exists():
        return []
    directory.mkdir(parents=True)
    written = [directory / README]
    written[0].write_text(SCHEDULE_README)
    for name, text in (default_files(config) if files is None else files).items():
        written.append(directory / f"{name}.md")
        written[-1].write_text(text)
    return written


def load_tasks(config: Config) -> tuple[list[Task], dict[str, str]]:
    directory = schedule_dir(config)
    tasks, problems = [], {}
    paths = sorted(directory.glob("*.md")) if directory.is_dir() else []
    for path in paths:
        if path.name == README:
            continue
        try:
            tasks.append(parse_task(path))
        except (OSError, ScheduleError) as error:
            problems[path.stem] = str(error)
    return tasks, problems


def state_path(config: Config) -> Path:
    return config.state_dir / "schedule.json"


def load_state(config: Config) -> dict:
    try:
        return json.loads(state_path(config).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(config: Config, state: dict) -> None:
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True))
    temporary.replace(path)


def local(timestamp: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(timestamp)


def latest_slot(task: Task, now: float) -> float | None:
    moment = local(now)
    for back in range(8):
        day = moment.date() - datetime.timedelta(days=back)
        if DAY_NAMES[day.weekday()] not in task.days:
            continue
        slot = datetime.datetime.combine(day, datetime.time(*task.at)).timestamp()
        if slot <= now:
            return slot
    return None


def next_slot(task: Task, after: float) -> float | None:
    moment = local(after)
    for ahead in range(8):
        day = moment.date() + datetime.timedelta(days=ahead)
        if DAY_NAMES[day.weekday()] not in task.days:
            continue
        slot = datetime.datetime.combine(day, datetime.time(*task.at)).timestamp()
        if slot > after:
            return slot
    return None


def expired(task: Task, now: float) -> bool:
    return task.until is not None and local(now).date() > task.until


def day_allowed(task: Task, now: float) -> bool:
    return DAY_NAMES[local(now).weekday()] in task.days


def is_due(task: Task, entry: dict, now: float) -> bool:
    if not task.enabled or expired(task, now):
        return False
    last = entry.get("last")
    if task.every:
        return day_allowed(task, now) and (last is None or now - last >= task.every)
    slot = latest_slot(task, now)
    reference = last if last is not None else entry.get("seen", now)
    return slot is not None and slot > reference


def next_due(task: Task, entry: dict, now: float) -> float | None:
    if not task.enabled or expired(task, now):
        return None
    if is_due(task, entry, now):
        return now
    if task.every:
        return (entry.get("last") or now) + task.every
    return next_slot(task, max(now, entry.get("last") or 0))


def run_command(config: Config, command: str) -> tuple[int, str]:
    env = {**os.environ, "GOPHER_HOME": str(config.root)}
    try:
        result = subprocess.run(command, shell=True, cwd=config.root, capture_output=True, text=True, timeout=RUN_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {RUN_TIMEOUT}s"
    output = (result.stdout + result.stderr).strip().splitlines()
    if len(output) > OUTPUT_LINES:
        output = [f"... ({len(output) - OUTPUT_LINES} earlier lines)", *output[-OUTPUT_LINES:]]
    return result.returncode, "\n".join(output)


def weekdays_between(start: datetime.date, end: datetime.date) -> int:
    days, day = 0, start
    while day < end:
        day += datetime.timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return days


def stale_projects(config: Config, now: float) -> list[tuple[str, str, int]]:
    today = local(now).date()
    stale = []
    directory = config.projects_dir
    for status in sorted(directory.glob("*/STATUS.md")) if directory.is_dir() else []:
        try:
            fields, _ = read_frontmatter(status.read_text())
            updated = datetime.date.fromisoformat(fields.get("updated", ""))
        except (OSError, ValueError):
            continue
        if fields.get("state", "active") not in OPEN_STATES:
            continue
        idle = weekdays_between(updated, today)
        if idle >= STALE_WEEKDAYS:
            stale.append((status.parent.name, updated.isoformat(), idle))
    return stale


def backup_event(config: Config, state: dict, now: float) -> str | None:
    status = rootgit.backup_status(config.root)
    overdue = status.level(now, save_interval_hours(config)) == "overdue"
    was = state.get("backup_overdue", False)
    state["backup_overdue"] = overdue
    if overdue and not was:
        return f"Backup overdue: {status.describe(now, save_interval_hours(config)).removeprefix('backup: ')}. Run `gopher save` if no save task ran it."
    return None


def save_interval_hours(config: Config) -> float:
    path = schedule_dir(config) / "save.md"
    if path.is_file():
        try:
            task = parse_task(path)
        except (OSError, ScheduleError):
            task = None
        if task and task.every and task.enabled:
            return task.every / 3600
        if task:
            return 0
    try:
        return gopher_save_every_hours(config)
    except ConfigError:
        return 0


@dataclass
class TickReport:
    sections: list[str]
    ran: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n\n".join(self.sections)


def tick(config: Config, now: float | None = None, dry_run: bool = False) -> TickReport:
    now = now or time.time()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with open(config.state_dir / "schedule.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return TickReport([])
        return locked_tick(config, now, dry_run)


def locked_tick(config: Config, now: float, dry_run: bool) -> TickReport:
    sections, ran = [], []
    if dry_run and not schedule_dir(config).exists():
        return TickReport([f"would create {schedule_dir(config)} with the default tasks"])
    seeded = seed(config)
    if seeded:
        names = ", ".join(path.name for path in seeded)
        sections.append(f"## Schedule created\nWrote {schedule_dir(config)} ({names}). Tell the user in one line.")
    state = load_state(config)
    entries = state.setdefault("tasks", {})
    tasks, problems = load_tasks(config)
    for task in tasks:
        entry = entries.setdefault(task.name, {})
        entry.setdefault("seen", now)
        if expired(task, now):
            if not entry.get("expired_reported"):
                entry["expired_reported"] = True
                sections.append(f"## {task.name} has ended\nIts `until` date ({task.until}) has passed. Ask the user whether to delete {task.path}.")
            continue
        if not is_due(task, entry, now):
            continue
        section = [f"## {task.name} ({task.timing()})"]
        speak = True
        if task.run:
            if dry_run:
                section.append(f"would run: {task.run}")
            else:
                code, output = run_command(config, task.run)
                previous = entry.get("output")
                entry["exit"], entry["output"] = code, output
                if task.notify == "failure":
                    speak = code != 0
                elif task.notify == "change":
                    speak = code != 0 or (previous is not None and output != previous)
                section.append(f"$ {task.run}  [exit {code}]" + (f"\n{output}" if output else ""))
                ran.append(f"{task.name} (exit {code})")
        elif not dry_run:
            ran.append(task.name)
        if task.body:
            section.append(task.body)
        if not dry_run:
            entry["last"] = now
        if speak:
            sections.append("\n".join(section))
    reported = state.setdefault("problems", {})
    for name, problem in problems.items():
        key = f"{problem}@{(schedule_dir(config) / f'{name}.md').stat().st_mtime}"
        if reported.get(name) != key:
            reported[name] = key
            sections.append(f"## Problem in schedule/{name}.md\n{problem}. Tell the user, or fix it if they asked for this task.")
    for name in set(reported) - set(problems):
        del reported[name]
    for name in set(entries) - {task.name for task in tasks}:
        del entries[name]
    events = []
    if event := backup_event(config, state, now):
        events.append(event)
    try:
        proactive = gopher_proactivity(config) == "proactive"
    except ConfigError:
        proactive = False
    if proactive:
        flagged = state.setdefault("stale", {})
        current = stale_projects(config, now)
        for project, updated, idle in current:
            if flagged.get(project) != updated:
                flagged[project] = updated
                events.append(f"Stale project: {project} (STATUS.md last updated {updated}, {idle} working days ago)")
        for project in set(flagged) - {project for project, _, _ in current}:
            del flagged[project]
    if events:
        sections.append("## Events\n" + "\n".join(f"- {event}" for event in events))
    state["last_tick"] = now
    if not dry_run:
        save_state(config, state)
    if sections:
        stamp = local(now).strftime("%Y-%m-%d %H:%M")
        sections.insert(0, f"# gopher schedule tick ({stamp})")
    return TickReport(sections, ran)


def report_path(config: Config) -> Path:
    return config.state_dir / REPORT_FILE


def queue_report(config: Config, text: str) -> None:
    path = report_path(config)
    existing = path.read_text().rstrip("\n") if path.is_file() else ""
    path.write_text(existing + ("\n\n" if existing else "") + text.strip() + "\n")


def take_report(config: Config) -> str:
    path = report_path(config)
    try:
        text = path.read_text()
    except OSError:
        return ""
    path.unlink(missing_ok=True)
    state = load_state(config)
    if state.pop("notified_at", None) is not None:
        save_state(config, state)
    return text


def input_is_empty(screen: str) -> bool:
    prompts = [line for line in screen.splitlines() if line.lstrip().startswith(PROMPT_MARK)]
    return bool(prompts) and prompts[-1].strip() == PROMPT_MARK


def tmux_out(*argv: str) -> str | None:
    try:
        result = subprocess.run(["tmux", *argv], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def gopher_pane() -> str | None:
    listing = tmux_out("list-panes", "-s", "-t", "=gopher:", "-F", "#{pane_id}\t#{pane_current_command}") or ""
    return next((pane for pane, _, command in (line.partition("\t") for line in listing.splitlines()) if command == "claude"), None)


def gopher_idle(config: Config, pane: str) -> bool:
    return any(s.get("pane") == pane and s.get("state") == "idle" for s in current_sessions(config, prune=False))


def deliver(config: Config, now: float) -> bool:
    if not report_path(config).is_file():
        return False
    state = load_state(config)
    if now - state.get("notified_at", 0) < RENOTIFY_SECONDS:
        return False
    pane = gopher_pane()
    if not pane or not gopher_idle(config, pane) or not input_is_empty(tmux_out("capture-pane", "-p", "-t", pane) or ""):
        return False
    if tmux_out("send-keys", "-t", pane, "-l", NOTIFY_TEXT) is None or tmux_out("send-keys", "-t", pane, "Enter") is None:
        return False
    state["notified_at"] = now
    save_state(config, state)
    return True


def loop_log(message: str) -> None:
    print(f"{time.strftime('%H:%M')}  {message}", flush=True)


def run_loop(config: Config, interval: int = LOOP_SECONDS, stop=None, clock=time.time, sleep=time.sleep, log=loop_log) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with open(config.state_dir / "schedule-run.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScheduleError("a schedule loop is already running for this root") from None
        log(f"schedule loop for {config.root}: ticking every {format_age(interval)}, Gopher hears only when something needs it")
        while not (stop and stop()):
            now = clock()
            if now - load_state(config).get("last_tick", 0) >= interval:
                report = tick(config, now)
                ran = ", ".join(report.ran) or "nothing due"
                if report.sections:
                    queue_report(config, report.text())
                    log(f"tick: ran {ran}; queued a report for Gopher")
                else:
                    log(f"tick: ran {ran}; nothing for Gopher")
            if deliver(config, now):
                log("told Gopher to read the report")
            sleep(DELIVERY_CHECK_SECONDS)


@dataclass
class Row:
    name: str
    timing: str
    last: float | None
    next: float | None
    run: str | None
    note: str = ""


def listing(config: Config, now: float | None = None) -> tuple[list[Row], dict[str, str]]:
    now = now or time.time()
    tasks, problems = load_tasks(config)
    entries = load_state(config).get("tasks", {})
    rows = []
    for task in tasks:
        entry = entries.get(task.name, {})
        note = "disabled" if not task.enabled else f"ended {task.until}" if expired(task, now) else ""
        if entry.get("exit"):
            note = (note + f" last exit {entry['exit']}").strip()
        rows.append(Row(task.name, task.timing(), entry.get("last"), next_due(task, entry, now), task.run, note))
    return rows, problems


def heartbeat_summary(config: Config, now: float | None = None) -> tuple[str, bool] | None:
    now = now or time.time()
    state = load_state(config)
    last = state.get("last_tick")
    if not schedule_dir(config).is_dir() and not last:
        return None
    if not last:
        return "no tick yet", False
    text = f"last tick {format_age(now - last)} ago"
    rows, _ = listing(config, now)
    upcoming = sorted((row for row in rows if row.next), key=lambda row: row.next)
    if upcoming:
        first = upcoming[0]
        text += f", next: {first.name} " + ("now" if first.next <= now else f"in {format_seconds(first.next - now)}")
    return text, now - last > HEARTBEAT_STALE_SECONDS


HEARTBEAT_ICON, NEXT_ICON = "\U0001f552", "\u27a1\ufe0f"


def heartbeat_compact(config: Config, now: float | None = None) -> tuple[str, bool] | None:
    now = now or time.time()
    last = load_state(config).get("last_tick")
    if not schedule_dir(config).is_dir() and not last:
        return None
    if not last:
        return f"{HEARTBEAT_ICON} off", False
    text = f"{HEARTBEAT_ICON} {format_age(now - last)}"
    rows, _ = listing(config, now)
    upcoming = sorted((row for row in rows if row.next and row.name not in BUILTIN_TASKS), key=lambda row: row.next)
    if upcoming:
        first = upcoming[0]
        text += f"  {NEXT_ICON}  {first.name} " + ("now" if first.next <= now else f"in {format_seconds(first.next - now)}")
    return text, now - last > HEARTBEAT_STALE_SECONDS


def format_seconds(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def format_age(seconds: float) -> str:
    return "0m" if seconds < 60 else format_seconds(seconds)
