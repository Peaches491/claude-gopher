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


def changed_paths(root: Path) -> list[str]:
    out = git(root, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    entries = out.split("\0")
    paths, skip = [], False
    for entry in entries:
        if skip:
            paths.append(entry)
            skip = False
            continue
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        skip = entry[0] in "RC"
    return paths


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
