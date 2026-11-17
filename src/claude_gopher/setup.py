import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import claude_gopher
from claude_gopher.checkouts import project_dirs
from claude_gopher.config import DEFAULT_ROOT, load_config, load_registry, save_registry
from claude_gopher.hooks import gopher_command, plan_hooks, read_settings, settings_path, unplan_hooks, write_settings
from claude_gopher.prompt import CONTEXTS, assemble
from claude_gopher.roots import RootError, check_name, check_target, create_root, is_root, register_root, repair_root

NEW, REVIEW = "new", "review"
COMMIT_SUBJECTS = {NEW: "Configure gopher root via setup interview", REVIEW: "Update gopher site config via setup review"}


@dataclass
class SetupIO:
    ask: Callable[[str, str | None], str]
    confirm: Callable[[str, bool], bool]
    say: Callable[[str], None]
    launch: Callable[[Path, str], None]


def install_dir() -> Path:
    return Path(claude_gopher.__file__).resolve().parent


def design_doc() -> Path | None:
    candidate = install_dir().parent.parent / "docs" / "design.md"
    return candidate if candidate.is_file() else None


def kickoff_message(root: Path, mode: str) -> str:
    config = load_config(root)
    package = install_dir()
    site_files = sorted(p.name for p in config.prompts_dir.glob("*.md") if p.name != "README.md")
    projects = [p.name for p in project_dirs(config.projects_dir)]
    lines = [
        f"Run the gopher setup interview for the root at `{root}` (mode: {mode}).",
        "",
        f"Gopher is installed at `{package}` (readable in this session). Before asking anything, read:",
        f"- `{package}/prompts/setup.md`: your interview script (also appended to your system prompt).",
        f"- `{package}/prompts/`: the core prompt for each context ({', '.join(CONTEXTS)}). Site files"
        " extend these; `gopher prompt <context>` shows the assembled result.",
        f"- `{package}/skeleton/`: what a fresh root contains.",
    ]
    if doc := design_doc():
        lines.append(f"- `{doc}`: the design, for background on roots, prompts, and checkouts.")
    lines += [
        f"- `{root}/config.toml` and `{config.prompts_dir}/`.",
        "",
        f"Existing site prompt files: {', '.join(site_files) if site_files else 'none yet'}.",
        f"Projects in this root: {len(projects)}{' (' + ', '.join(projects[:10]) + ')' if projects else ''}.",
        "",
    ]
    if mode == REVIEW:
        lines.append(
            "This is a review: summarize what the existing site files and config say, ask what has changed"
            " or is missing, and update only what the user confirms."
        )
    else:
        lines.append("This is a new root: walk through every topic in the script, skipping what the user says does not apply.")
    lines.append(f"Commit subject for this session: `{COMMIT_SUBJECTS[mode]}`.")
    lines.append("When you have read the files, start with the first question.")
    return "\n".join(lines)


def launch_interview(root: Path, mode: str) -> None:
    system = assemble(load_config(root), "setup")
    message = kickoff_message(root, mode)
    os.chdir(root)
    os.environ["GOPHER_HOME"] = str(root)
    os.execvp("claude", ["claude", "--add-dir", str(install_dir()), "--append-system-prompt", system, message])


def registered_name(registry, target: Path) -> str | None:
    return next((name for name, path in registry.roots.items() if path == target), None)


def run_setup(args, io: SetupIO, settings: Path | None = None) -> int:
    if args.remove_hooks:
        remove_hooks(settings or settings_path(), io, args.yes)
        return 0
    registry = load_registry()
    default_dir = str(registry.roots[registry.default]) if registry.default in registry.roots else DEFAULT_ROOT
    target = Path(args.dir or io.ask("Root directory", default_dir)).expanduser().resolve()

    if args.review:
        if not is_root(target):
            raise RootError(f"{target} is not a gopher root; run `gopher setup {target}` first")
        io.launch(target, REVIEW)
        return 0

    existing = is_root(target)
    if existing and args.source:
        raise RootError(f"{target} is already a gopher root; --from only applies to new roots")
    if not existing:
        check_target(target)
    name = registered_name(registry, target) or args.name or io.ask("Root name", target.name)
    check_name(name, target, registry)

    if existing:
        for fix in repair_root(target):
            io.say(fix)
        io.say(f"using existing root {target}")
    else:
        create_root(target, args.source)
        io.say(f"{'cloned' if args.source else 'created'} root at {target}")

    make_default = args.default
    if make_default is None and registry.default and registry.default != name:
        make_default = io.confirm(f"Make {name!r} the default root (current: {registry.default})?", False)
    register_root(registry, name, target, bool(make_default))
    save_registry(registry)
    io.say(f"registered root {name!r}" + (" (default)" if registry.default == name else ""))

    if not args.no_hooks:
        install_hooks(settings or settings_path(), io, args.yes)

    if args.no_interview:
        return 0
    if io.confirm("Run the setup interview with Claude now?", not existing):
        io.launch(target, REVIEW if existing else NEW)
    else:
        io.say("run `gopher setup --review` any time to configure this root's prompts")
    return 0


def remove_hooks(path: Path, io: SetupIO, yes: bool) -> None:
    updated, removed = unplan_hooks(read_settings(path))
    if not removed:
        io.say(f"no gopher hooks in {path}")
        return
    io.say(f"hooks to remove from {path}:\n" + "\n".join(f"  {line}" for line in removed))
    if not (yes or io.confirm("Remove these hooks?", True)):
        io.say("left the hooks in place")
        return
    backup = write_settings(path, updated)
    io.say("removed gopher hooks" + (f" (backup: {backup})" if backup else ""))


def install_hooks(path: Path, io: SetupIO, yes: bool) -> None:
    updated, added = plan_hooks(read_settings(path), gopher_command())
    if not added:
        io.say(f"gopher hooks already installed in {path}")
        return
    io.say(f"hooks to add to {path}:\n" + "\n".join(f"  {line}" for line in added))
    if not (yes or io.confirm("Install these hooks?", True)):
        io.say("skipped hooks; `gopher hook state --print-settings` and `gopher hook context --print-settings` show them")
        return
    backup = write_settings(path, updated)
    io.say("installed hooks" + (f" (backup: {backup})" if backup else ""))
