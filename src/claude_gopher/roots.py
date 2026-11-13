import datetime
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from claude_gopher.config import DEFAULT_PATHS, ROOT_CONFIG, Registry, load_config

SKELETON_DIRS = ("projects_dir", "archived_dir", "closed_dir", "agent_dir", "reference_dir")
PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RootError(Exception):
    pass


def check_target(target: Path) -> None:
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise RootError(f"{target} exists and is not an empty directory")


def check_name(name: str, target: Path, registry: Registry) -> None:
    if not PROJECT_NAME.match(name):
        raise RootError(f"invalid root name {name!r}")
    if name in registry.roots and registry.roots[name] != target:
        raise RootError(f"root name {name!r} is already registered for {registry.roots[name]}")


def git(*args: str, cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RootError(f"git {' '.join(args)} failed: {result.stderr.strip()}")


def copy_skeleton(target: Path) -> None:
    skeleton = resources.files("claude_gopher") / "skeleton"
    with resources.as_file(skeleton) as source:
        shutil.copytree(source, target, dirs_exist_ok=True)
    (target / "gitignore").rename(target / ".gitignore")
    for key in SKELETON_DIRS:
        directory = target / DEFAULT_PATHS[key]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch()


TMP_IGNORE = "/tmp/"


def tmp_ignored(target: Path) -> bool:
    gitignore = target / ".gitignore"
    lines = {line.strip() for line in gitignore.read_text().splitlines()} if gitignore.is_file() else set()
    if lines & {"tmp", "tmp/", "/tmp", "/tmp/"}:
        return True
    probe = target / "tmp" / ".gopher-ignore-probe"
    result = subprocess.run(
        ["git", "-C", str(target), "check-ignore", "-q", str(probe.relative_to(target))],
        capture_output=True,
    )
    return result.returncode == 0


def ensure_tmp_ignored(target: Path) -> bool:
    if tmp_ignored(target):
        return False
    gitignore = target / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.is_file() else []
    gitignore.write_text("\n".join([*lines, TMP_IGNORE]) + "\n")
    return True


def create_root(target: Path, source_url: str | None = None) -> None:
    check_target(target)
    if source_url:
        target.parent.mkdir(parents=True, exist_ok=True)
        git("clone", source_url, str(target), cwd=target.parent)
        if not (target / ROOT_CONFIG).is_file():
            raise RootError(f"cloned {source_url} but it has no {ROOT_CONFIG}; is it a gopher root?")
        ensure_tmp_ignored(target)
    else:
        target.mkdir(parents=True, exist_ok=True)
        copy_skeleton(target)
        git("init", "-q", cwd=target)
        git("add", "-A", cwd=target)
        git("commit", "-q", "-m", "Create gopher root", cwd=target)
    load_config(target).state_dir.mkdir(parents=True, exist_ok=True)


def is_root(target: Path) -> bool:
    return (target / ROOT_CONFIG).is_file()


def repair_root(target: Path) -> list[str]:
    fixed = []
    state_dir = load_config(target).state_dir
    if not state_dir.is_dir():
        state_dir.mkdir(parents=True)
        fixed.append(f"created {state_dir}")
    if ensure_tmp_ignored(target):
        fixed.append("added /tmp/ to .gitignore")
    return fixed


def register_root(registry: Registry, name: str, target: Path, make_default: bool) -> None:
    registry.roots[name] = target
    if make_default or not registry.default:
        registry.default = name


def create_project(projects_dir: Path, name: str, summary: str = "", today: datetime.date | None = None) -> Path:
    if not PROJECT_NAME.match(name):
        raise RootError(f"invalid project name {name!r}")
    project_dir = projects_dir / name
    if project_dir.exists():
        raise RootError(f"project {name!r} already exists at {project_dir}")
    project_dir.mkdir(parents=True)
    today = today or datetime.date.today()
    (project_dir / "STATUS.md").write_text(
        f"---\nsummary: {summary}\nstate: active\nupdated: {today.isoformat()}\n---\n# {name}\n"
    )
    return project_dir
