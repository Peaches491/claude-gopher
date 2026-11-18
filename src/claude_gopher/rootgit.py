import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from claude_gopher.config import Config

ROOT_GROUP = "root"
IN_PROGRESS = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply")
AREA_NAMES = {"STATUS.md": "STATUS", "human": "human notes", "checkouts.toml": "checkouts"}


class SaveError(Exception):
    pass


@dataclass
class SaveReport:
    commits: list[str] = field(default_factory=list)
    pushed: bool = False
    push_output: str = ""


def git(root: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SaveError(result.stderr.strip() or f"git {argv[0]} failed")
    return result


def status_entries(root: Path) -> list[tuple[str, list[str]]]:
    out = git(root, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    entries, rows = iter(out.split("\0")), []
    for entry in entries:
        if len(entry) < 4:
            continue
        paths = [entry[3:]]
        if entry[0] in "RC":
            paths.append(next(entries, ""))
        rows.append((entry[:2], paths))
    return rows


def changed_paths(root: Path) -> list[str]:
    return [path for _, paths in status_entries(root) for path in paths]


UNTRACKED_LINES_LIMIT = 1 << 20


def file_lines(path: Path) -> int:
    try:
        if path.stat().st_size > UNTRACKED_LINES_LIMIT:
            return 0
        data = path.read_bytes()
    except OSError:
        return 0
    if b"\0" in data:
        return 0
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def line_counts(root: Path, rows: list[tuple[str, list[str]]]) -> tuple[int, int]:
    added = removed = 0
    for line in git(root, "diff", "HEAD", "--numstat", check=False).stdout.splitlines():
        plus, minus, _ = line.split("\t", 2)
        if plus.isdigit() and minus.isdigit():
            added, removed = added + int(plus), removed + int(minus)
    added += sum(file_lines(root / paths[0]) for code, paths in rows if code == "??")
    return added, removed


def lifecycle_dirs(config: Config) -> list[str]:
    dirs = []
    for directory in (config.projects_dir, config.archived_dir, config.closed_dir):
        try:
            dirs.append(directory.relative_to(config.root).as_posix())
        except ValueError:
            continue
    return dirs


def group_of(path: str, dirs: list[str]) -> tuple[str, str]:
    for directory in dirs:
        prefix = directory.rstrip("/") + "/"
        if path.startswith(prefix):
            rest = path[len(prefix):].split("/")
            if len(rest) > 1:
                return f"{prefix}{rest[0]}", rest[1]
    return ROOT_GROUP, path.split("/")[0]


def group_changes(config: Config, paths: list[str]) -> dict[str, set[str]]:
    dirs = lifecycle_dirs(config)
    groups: dict[str, set[str]] = {}
    for path in paths:
        group, area = group_of(path, dirs)
        groups.setdefault(group, set()).add(area)
    return groups


def commit_message(group: str, areas: set[str]) -> str:
    names = sorted(AREA_NAMES.get(area, area.removesuffix(".md")) for area in areas)
    label = group.rsplit("/", 1)[-1] if group != ROOT_GROUP else ROOT_GROUP
    return f"{label}: update {', '.join(names)}"


def busy(root: Path) -> str | None:
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    return next((marker for marker in IN_PROGRESS if (git_dir / marker).exists()), None)


def save(config: Config, push: bool = True, dry_run: bool = False) -> SaveReport:
    root = config.root
    if git(root, "rev-parse", "--show-toplevel", check=False).stdout.strip() != str(root.resolve()):
        raise SaveError(f"{root} is not its own git repository; run git init there (and add a remote) to save it")
    if marker := busy(root):
        raise SaveError(f"a git operation is in progress in {root} ({marker}); finish it first")
    groups = group_changes(config, changed_paths(root))
    report = SaveReport()
    ordered = sorted(groups.items(), key=lambda item: (item[0] == ROOT_GROUP, item[0]))
    for group, areas in ordered:
        message = commit_message(group, areas)
        report.commits.append(message)
        if dry_run:
            continue
        pathspec = ["-A"] if group == ROOT_GROUP else ["-A", "--", group]
        git(root, "add", *pathspec)
        if git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
            report.commits.pop()
            continue
        git(root, "commit", "-q", "-m", message)
    if push and not dry_run and (report.commits or ahead(root)):
        result = git(root, "push", "-q", check=False)
        if result.returncode != 0:
            raise SaveError(f"committed {len(report.commits)} change(s) but push failed: {result.stderr.strip()}")
        report.pushed = True
        report.push_output = result.stderr.strip()
    return report


def ahead(root: Path) -> bool:
    result = git(root, "rev-list", "--count", "@{upstream}..HEAD", check=False)
    return result.returncode == 0 and result.stdout.strip() not in ("", "0")


BACKUP_GRACE_SECONDS = 30 * 60


@dataclass
class BackupStatus:
    last_commit: float | None = None
    changed: int = 0
    added: int = 0
    removed: int = 0
    unpushed: int | None = None
    error: str | None = None

    def counts(self) -> str:
        return f"+{self.added} -{self.removed} ~{self.changed}"

    def overdue(self, now: float, every_hours: float) -> bool:
        if not every_hours or not (self.changed or self.unpushed):
            return False
        return self.last_commit is None or now - self.last_commit > every_hours * 3600 + BACKUP_GRACE_SECONDS

    def level(self, now: float, every_hours: float) -> str:
        if self.error or self.unpushed is None:
            return "error"
        return "overdue" if self.overdue(now, every_hours) else "ok"

    def describe(self, now: float, every_hours: float) -> str:
        if self.error:
            return f"backup: {self.error}"
        parts = [f"saved {age(now - self.last_commit)} ago" if self.last_commit else "never saved"]
        if self.overdue(now, every_hours):
            parts[0] += f" (due every {every_hours:g}h)"
        if self.changed:
            parts.append(f"unsaved {self.counts()}")
        if self.unpushed is None:
            parts.append("no upstream to push to")
        elif self.unpushed:
            parts.append(f"{self.unpushed} unpushed")
        elif not self.changed:
            parts.append("pushed")
        return "backup: " + ", ".join(parts)


def age(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def backup_status(root: Path) -> BackupStatus:
    if git(root, "rev-parse", "--show-toplevel", check=False).stdout.strip() != str(root.resolve()):
        return BackupStatus(error="the root is not its own git repository")
    try:
        rows = status_entries(root)
    except SaveError as error:
        return BackupStatus(error=str(error))
    stamp = git(root, "log", "-1", "--format=%ct", check=False).stdout.strip()
    count = git(root, "rev-list", "--count", "@{upstream}..HEAD", check=False)
    unpushed = int(count.stdout.strip()) if count.returncode == 0 and count.stdout.strip().isdigit() else None
    added, removed = line_counts(root, rows) if rows else (0, 0)
    return BackupStatus(
        last_commit=float(stamp) if stamp else None, changed=len(rows), added=added, removed=removed, unpushed=unpushed,
    )
