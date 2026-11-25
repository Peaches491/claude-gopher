import datetime
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from claude_gopher.checkouts import all_checkouts, project_dirs, read_checkouts
from claude_gopher.config import Config
from claude_gopher.lifecycle import git, is_protected, protected_patterns, worktree_info
from claude_gopher.overview import current_sessions
from claude_gopher.projects import read_frontmatter
from claude_gopher.schedule import local, weekdays_between

STALE_WORKDAYS = 15
LARGE_BYTES = 100 * 1024 * 1024
DU_TIMEOUT = 120
STATUS_TIMEOUT = 20


@dataclass
class StaleProject:
    name: str
    updated: str
    idle: int
    state: str
    running: bool


@dataclass
class Orphan:
    path: Path
    main: Path
    branch: str | None
    last_commit: float | None
    blockers: list[str] = field(default_factory=list)
    former: list[str] = field(default_factory=list)
    missing: bool = False


@dataclass
class TmpDir:
    path: Path
    size: int
    reason: str


@dataclass
class Report:
    stale: list[StaleProject]
    orphans: list[Orphan]
    tmp: list[TmpDir]


def home(path: Path) -> str:
    text = str(path)
    prefix = str(Path.home())
    return "~" + text[len(prefix):] if text == prefix or text.startswith(prefix + "/") else text


def age(seconds: float) -> str:
    days = int(seconds // 86400)
    if days < 1:
        return "today"
    if days < 14:
        return f"{days}d ago"
    if days < 60:
        return f"{days // 7}w ago"
    return f"{days // 30}mo ago"


def size_text(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def stale_projects(config: Config, now: float, workdays: int) -> list[StaleProject]:
    today = local(now).date()
    running = {s.get("project") for s in current_sessions(config, prune=False)}
    stale = []
    for directory in project_dirs(config.projects_dir):
        try:
            fields, _ = read_frontmatter((directory / "STATUS.md").read_text())
            updated = datetime.date.fromisoformat(fields.get("updated", ""))
        except (OSError, ValueError):
            continue
        idle = weekdays_between(updated, today)
        if idle >= workdays:
            stale.append(StaleProject(directory.name, updated.isoformat(), idle, fields.get("state", "active"), directory.name in running))
    return sorted(stale, key=lambda p: -p.idle)


def blockers(path: Path) -> list[str]:
    found = []
    try:
        status = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=STATUS_TIMEOUT,
        )
        if status.returncode == 0 and status.stdout.strip():
            found.append(f"uncommitted changes ({len(status.stdout.splitlines())} files)")
    except subprocess.TimeoutExpired:
        found.append(f"changes unknown (git status took over {STATUS_TIMEOUT}s)")
    unpushed = git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if unpushed.returncode == 0 and unpushed.stdout.strip() not in ("", "0"):
        found.append(f"{unpushed.stdout.strip()} commit(s) not on any remote branch")
    return found


def worktree_paths(main: Path) -> list[tuple[Path, bool]]:
    listing = git(main, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return []
    entries, current = [], None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            current = [Path(line.removeprefix("worktree ")), False]
            entries.append(current)
        elif line.startswith("prunable") and current:
            current[1] = True
    return [(path, prunable) for path, prunable in entries]


def former_owners(config: Config) -> dict[Path, list[str]]:
    owners: dict[Path, list[str]] = {}
    for directory in (config.archived_dir, config.closed_dir):
        for project in project_dirs(directory):
            for checkout in read_checkouts(project):
                owners.setdefault(checkout, []).append(f"{project.name} ({directory.name})")
    return owners


def orphaned_worktrees(config: Config) -> list[Orphan]:
    owned = {path.resolve() for _, path in all_checkouts(config.projects_dir)}
    former = former_owners(config)
    known = [*owned, *(path.resolve() for path in former)]
    mains = set()
    for path in known:
        if path.is_dir():
            main, _ = worktree_info(path)
            mains.add(main or path)
    protected = protected_patterns(config)
    found: dict[Path, tuple[Path, bool]] = {}
    for main in sorted(mains):
        for path, prunable in worktree_paths(main):
            if path.resolve() != main.resolve() and path.resolve() not in owned and not is_protected(path, protected):
                found.setdefault(path, (main, prunable))

    def inspect(item: tuple[Path, tuple[Path, bool]]) -> Orphan:
        path, (main, prunable) = item
        previous = former.get(path, [])
        if prunable or not path.is_dir():
            return Orphan(path, main, None, None, former=previous, missing=True)
        _, branch = worktree_info(path)
        stamp = git(path, "log", "-1", "--format=%ct").stdout.strip()
        return Orphan(path, main, branch, float(stamp) if stamp else None, blockers(path), previous)

    with ThreadPoolExecutor(max_workers=8) as pool:
        orphans = list(pool.map(inspect, found.items()))
    return sorted(orphans, key=lambda o: (o.missing, o.last_commit or 0))


def tmp_dirs(config: Config, large: int) -> list[TmpDir]:
    tmp = config.root / "tmp"
    if not tmp.is_dir():
        return []
    open_projects = {d.name for d in project_dirs(config.projects_dir)}
    entries = sorted(p for p in tmp.iterdir() if p.is_dir())
    try:
        result = subprocess.run(["du", "-sk", *map(str, entries)], capture_output=True, text=True, timeout=DU_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return []
    sizes = {}
    for line in result.stdout.splitlines():
        kilobytes, _, path = line.partition("\t")
        if kilobytes.isdigit():
            sizes[Path(path)] = int(kilobytes) * 1024
    found = []
    for path in entries:
        size = sizes.get(path, 0)
        if path.name != "gopher" and path.name not in open_projects and size:
            found.append(TmpDir(path, size, "no open project by that name"))
        elif size >= large:
            found.append(TmpDir(path, size, "large"))
    return sorted(found, key=lambda t: -t.size)


def report(config: Config, now: float | None = None, workdays: int = STALE_WORKDAYS, large: int = LARGE_BYTES) -> Report:
    now = now or time.time()
    return Report(stale_projects(config, now, workdays), orphaned_worktrees(config), tmp_dirs(config, large))


def render(result: Report, now: float, workdays: int, large: int) -> list[str]:
    lines = []
    if result.stale:
        lines.append(f"Stale projects (STATUS.md not updated in {workdays}+ working days):")
        for project in result.stale:
            agent = ", agent running" if project.running else ""
            lines.append(f"  {project.name}  {project.state}, updated {project.updated} ({project.idle} working days){agent}")
        lines.append("  -> ask Gopher to archive or close them (gopher prompt archive / gopher prompt close)")
    if result.orphans:
        if lines:
            lines.append("")
        lines.append("Worktrees no open project owns:")
        for orphan in result.orphans:
            if orphan.missing:
                lines.append(f"  {home(orphan.path)}  directory gone; git -C {shlex.quote(home(orphan.main))} worktree prune")
                continue
            details = [f"branch {orphan.branch}" if orphan.branch else "detached"]
            if orphan.last_commit:
                details.append(f"last commit {age(now - orphan.last_commit)}")
            details += orphan.blockers or ["clean"]
            if orphan.former:
                details.append("was " + ", ".join(orphan.former))
            lines.append(f"  {home(orphan.path)}  {'; '.join(details)}")
        lines.append("  -> keep one: gopher checkout add <project> <path>; drop a clean one: git -C <main> worktree remove <path>")
    if result.tmp:
        if lines:
            lines.append("")
        lines.append(f"Temp folders ({size_text(large)}+, or for no open project):")
        for entry in result.tmp:
            lines.append(f"  {home(entry.path)}  {size_text(entry.size)}  {entry.reason}")
        lines.append("  -> they hold only regenerable files; delete with rm -rf <path>")
    return lines or ["Nothing to clean up."]
