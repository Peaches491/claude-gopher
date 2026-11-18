import re
from importlib import resources
from pathlib import Path

from claude_gopher.config import Config, gopher_features, gopher_proactivity, gopher_save_every_hours, gopher_update_check

CONTEXTS = ("all", "gopher", "project", "resources", "archive", "close", "reopen", "inbox", "tui-summary", "tui-detail", "setup")
SESSION_CONTEXTS = {"gopher": ("all", "gopher"), "project": ("all", "project")}
CONTEXT_LIMIT = 10_000
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def core_text(context: str) -> str:
    return (resources.files("claude_gopher") / "prompts" / f"{context}.md").read_text()


def site_text(config: Config, context: str) -> str:
    path = config.prompts_dir / f"{context}.md"
    return path.read_text() if path.is_file() else ""


def variables(config: Config, project: str | None) -> dict[str, str]:
    values = {
        "root": config.root,
        "projects_dir": config.projects_dir,
        "archived_dir": config.archived_dir,
        "closed_dir": config.closed_dir,
        "agent_dir": config.agent_dir,
        "prompts_dir": config.prompts_dir,
        "reference_dir": config.reference_dir,
        "schedule_dir": config.schedule_dir,
        "save_every_hours": gopher_save_every_hours(config),
        "update_check": "on" if gopher_update_check(config) else "off",
        "tmp_dir": config.root / "tmp",
        "project": project or "<project>",
        "project_dir": config.projects_dir / (project or "<project>"),
        "project_tmp": config.root / "tmp" / (project or "<project>"),
    }
    return {key: str(value) for key, value in values.items()}


def render(text: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def assemble(config: Config, context: str, project: str | None = None) -> str:
    if context not in CONTEXTS:
        raise ValueError(f"unknown context {context!r}; one of: {', '.join(CONTEXTS)}")
    values = variables(config, project)
    text = render(core_text(context), values).rstrip()
    if context == "gopher":
        sections = [f"gopher-proactivity-{gopher_proactivity(config)}"]
        sections += [f"gopher-feature-{feature}" for feature in gopher_features(config)]
        text += "".join("\n\n" + render(core_text(name), values).rstrip() for name in sections)
    site = site_text(config, context).strip()
    if site:
        text += "\n\n## Site\n\n" + render(site, values)
    return text + "\n"


def session_context(config: Config, kind: str, project: str | None) -> str:
    text = "\n".join(assemble(config, context, project) for context in SESSION_CONTEXTS[kind])
    if len(text) <= CONTEXT_LIMIT:
        return text
    note = f"\n\n[truncated; run `gopher prompt {' / '.join(SESSION_CONTEXTS[kind])}` for the full text]\n"
    return text[: CONTEXT_LIMIT - len(note)] + note


def is_gopher_session(config: Config, cwd: str, env: dict) -> bool:
    if env.get("GOPHER_AGENT") == "gopher":
        return True
    path = Path(cwd) if cwd else None
    return bool(path) and (path == config.agent_dir or config.agent_dir in path.parents)
