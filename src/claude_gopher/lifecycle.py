import fnmatch
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from claude_gopher import restore
from claude_gopher.checkouts import all_checkouts, all_shared, read_checkouts
from claude_gopher.config import Config

ACTIONS = ("archive", "close", "reopen")
COMMAND_TIMEOUT = 1800


class LifecycleError(Exception):
    pass


@dataclass
class CheckoutPlan:
    path: Path
    disposition: str
    reason: str = ""
    branch: str | None = None
    main: Path | None = None
    blockers: list[str] = field(default_factory=list)

    @property
    def cleaned(self) -> bool:
        return self.disposition in ("clean", "remove")


@dataclass
class Plan:
    action: str
    project: str
    source: Path
    destination: Path
    window: str | None
    commands: list[str]
    checkouts: list[CheckoutPlan] = field(default_factory=list)
    on_remove: list[str] = field(default_factory=list)

    @property
    def blockers(self) -> list[str]:
        return [f"{c.path}: {b}" for c in self.checkouts for b in c.blockers]


def git(path: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *argv], capture_output=True, text=True)


def lifecycle_settings(config: Config) -> dict:
    return config.settings.get("lifecycle", {})


def protected_patterns(config: Config) -> list[str]:
    return [str(Path(p).expanduser()) for p in config.settings.get("checkouts", {}).get("protected", [])]


def is_protected(path: Path, patterns: list[str]) -> bool:
    text = str(path)
    return any(fnmatch.fnmatch(text, pattern) or text == pattern or text.startswith(pattern.rstrip("/") + "/") for pattern in patterns)


def worktree_info(path: Path) -> tuple[Path | None, str | None]:
    git_dir = git(path, "rev-parse", "--absolute-git-dir")
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_dir.returncode != 0 or common.returncode != 0:
        return None, None
    if Path(git_dir.stdout.strip()) == Path(common.stdout.strip()):
        return None, None
    branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    return Path(common.stdout.strip()).parent, branch.stdout.strip() or None


def removal_blockers(path: Path) -> list[str]:
    blockers = []
    status = git(path, "status", "--porcelain")
    if status.returncode != 0:
        return [f"git status failed: {status.stderr.strip()}"]
    if status.stdout.strip():
        blockers.append(f"uncommitted changes ({len(status.stdout.splitlines())} paths)")
    unpushed = git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if unpushed.returncode == 0 and unpushed.stdout.strip() not in ("", "0"):
        blockers.append(f"{unpushed.stdout.strip()} commit(s) not on any remote branch")
    return blockers


def source_dir(config: Config, action: str, project: str) -> Path:
    if action == "reopen":
        for directory in (config.archived_dir, config.closed_dir):
            if (directory / project).is_dir():
                return directory / project
        raise LifecycleError(f"no archived or closed project named {project}")
    path = config.projects_dir / project
    if not path.is_dir():
        raise LifecycleError(f"no open project named {project}")
    return path


def destination_dir(config: Config, action: str, project: str) -> Path:
    return {"archive": config.archived_dir, "close": config.closed_dir, "reopen": config.projects_dir}[action] / project


def plan_checkout(config: Config, action: str, project: str, path: Path, patterns: list[str]) -> CheckoutPlan:
    if action == "reopen":
        return CheckoutPlan(path, "keep", "reopen does not touch checkouts" if path.is_dir() else "missing; fix with gopher checkout")
    if not path.is_dir():
        return CheckoutPlan(path, "skip", "missing")
    if is_protected(path, patterns):
        return CheckoutPlan(path, "keep", "protected")
    if path in all_shared(config.projects_dir):
        return CheckoutPlan(path, "keep", "shared; left as is")
    others = sorted({name for name, owned in all_checkouts(config.projects_dir) if owned == path and name != project})
    if others:
        return CheckoutPlan(path, "keep", f"also owned by {', '.join(others)}")
    if action == "archive":
        return CheckoutPlan(path, "clean", "kept; archive cleanup runs here")
    main, branch = worktree_info(path)
    if not main:
        return CheckoutPlan(path, "clean", "not a git worktree, so it is kept; close cleanup runs here")
    return CheckoutPlan(path, "remove", "worktree removed after cleanup", branch=branch, main=main, blockers=removal_blockers(path))


def make_plan(config: Config, action: str, project: str) -> Plan:
    if action not in ACTIONS:
        raise LifecycleError(f"unknown action {action}")
    source = source_dir(config, action, project)
    destination = destination_dir(config, action, project)
    if destination.exists():
        raise LifecycleError(f"{destination} already exists")
    commands = [] if action == "reopen" else list(lifecycle_settings(config).get(f"on_{action}", []))
    window = None if action == "reopen" else restore.find_window(project)
    patterns = protected_patterns(config)
    checkouts = [plan_checkout(config, action, project, path, patterns) for path in read_checkouts(source)]
    on_remove = list(lifecycle_settings(config).get("on_remove", [])) if action == "close" else []
    return Plan(action, project, source, destination, window, commands, checkouts, on_remove)


def describe(plan: Plan) -> str:
    verb = {"archive": "Archive", "close": "Close", "reopen": "Reopen"}[plan.action]
    lines = [f"{verb} {plan.project}: move {plan.source} -> {plan.destination}"]
    if plan.window:
        lines.append(f"- its tmux window {plan.window} is snapshotted and closed (the agent stops)")
    for checkout in plan.checkouts:
        lines.append(f"- {checkout.path}: {checkout.reason}")
        if checkout.cleaned and plan.commands:
            lines.append(f"    runs: {'; '.join(plan.commands)}")
        if checkout.disposition == "remove":
            lines.append(f"    git worktree remove{f', then git branch -d {checkout.branch}' if checkout.branch else ''}")
            if plan.on_remove:
                lines.append(f"    then: {'; '.join(plan.on_remove)}")
        for blocker in checkout.blockers:
            lines.append(f"    BLOCKED: {blocker}")
    if not plan.checkouts:
        lines.append("- no checkouts")
    if plan.blockers:
        lines.append("Resolve the blocked items first; nothing has changed.")
    return "\n".join(lines)


def run_command(command: str, cwd: Path, env: dict[str, str]) -> str | None:
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"{command!r} timed out in {cwd}"
    if result.returncode != 0:
        return f"{command!r} failed in {cwd}: {(result.stderr or result.stdout).strip()[-300:]}"
    return None


def execute(config: Config, plan: Plan) -> list[str]:
    if plan.blockers:
        raise LifecycleError("blocked:\n" + "\n".join(plan.blockers))
    report = []
    if plan.window:
        try:
            restore.close_window(config, plan.project)
            report.append("window snapshotted and closed")
        except restore.RestoreError as error:
            restore.tmux("kill-window", "-t", plan.window, check=False)
            report.append(f"window closed without a snapshot ({error})")
    for checkout in plan.checkouts:
        if not checkout.cleaned:
            continue
        env = {"GOPHER_PROJECT": plan.project, "GOPHER_CHECKOUT": str(checkout.path), "GOPHER_ROOT": str(config.root)}
        for command in plan.commands:
            if problem := run_command(command, checkout.path, env):
                report.append(problem)
        if checkout.disposition == "remove":
            removed = git(checkout.main, "worktree", "remove", str(checkout.path))
            if removed.returncode != 0:
                report.append(f"kept {checkout.path}: git worktree remove failed: {removed.stderr.strip()}")
                continue
            report.append(f"removed worktree {checkout.path}")
            if checkout.branch:
                deleted = git(checkout.main, "branch", "-d", checkout.branch)
                report.append(f"deleted branch {checkout.branch}" if deleted.returncode == 0 else f"kept branch {checkout.branch}: {deleted.stderr.strip()}")
            for command in plan.on_remove:
                if problem := run_command(command, checkout.main, env):
                    report.append(problem)
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(plan.source), str(plan.destination))
    report.append(f"moved to {plan.destination}")
    return report
