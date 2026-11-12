import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = "~/workon"
DEFAULT_PATHS = {
    "projects_dir": "projects",
    "archived_dir": "archived",
    "closed_dir": "closed",
    "agent_dir": "gopher",
    "state_dir": "tmp/gopher",
    "prompts_dir": "prompts",
}
DEFAULT_CLAUDE_SESSIONS_DIR = "~/.claude/sessions"
ROOT_CONFIG = "config.toml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    root: Path
    projects_dir: Path
    archived_dir: Path
    closed_dir: Path
    agent_dir: Path
    state_dir: Path
    prompts_dir: Path
    claude_sessions_dir: Path
    settings: dict = field(default_factory=dict, compare=False)

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"


@dataclass
class Registry:
    default: str | None = None
    roots: dict[str, Path] = field(default_factory=dict)


def registry_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(xdg).expanduser() / "claude-gopher" / "config.toml"


def load_registry(path: Path | None = None) -> Registry:
    path = path or registry_path()
    raw = tomllib.loads(path.read_text()) if path.is_file() else {}
    roots = {name: Path(value).expanduser() for name, value in raw.get("roots", {}).items()}
    return Registry(default=raw.get("default"), roots=roots)


def save_registry(registry: Registry, path: Path | None = None) -> None:
    path = path or registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if registry.default:
        lines.append(f"default = {json.dumps(registry.default)}")
    lines.append("")
    lines.append("[roots]")
    lines.extend(f"{json.dumps(name)} = {json.dumps(str(root))}" for name, root in registry.roots.items())
    path.write_text("\n".join(lines) + "\n")


def lookup_root(value: str, registry: Registry) -> Path:
    if value in registry.roots:
        return registry.roots[value]
    if "/" in value or value.startswith(("~", ".")):
        return Path(value).expanduser().resolve()
    raise ConfigError(f"unknown root {value!r}; registered: {', '.join(registry.roots) or 'none'}")


def containing_root(cwd: str, registry: Registry) -> Path | None:
    path = Path(cwd)
    matches = [root for root in registry.roots.values() if path == root or root in path.parents]
    return max(matches, key=lambda root: len(root.parts)) if matches else None


def resolve_root(
    root_arg: str | None = None,
    env: dict | None = None,
    cwd: str | None = None,
    registry: Registry | None = None,
) -> Path:
    env = os.environ if env is None else env
    registry = registry or load_registry()
    if root_arg:
        return lookup_root(root_arg, registry)
    if home := env.get("GOPHER_HOME"):
        return lookup_root(home, registry)
    if cwd and (root := containing_root(cwd, registry)):
        return root
    if registry.default:
        return lookup_root(registry.default, registry)
    return Path(DEFAULT_ROOT).expanduser()


def load_config(root: Path) -> Config:
    config_file = root / ROOT_CONFIG
    raw = tomllib.loads(config_file.read_text()) if config_file.is_file() else {}
    paths = {**DEFAULT_PATHS, **raw.get("paths", {})}
    return Config(
        root=root,
        **{key: root / Path(value).expanduser() for key, value in paths.items() if key in DEFAULT_PATHS},
        claude_sessions_dir=Path(
            raw.get("claude", {}).get("sessions_dir", DEFAULT_CLAUDE_SESSIONS_DIR)
        ).expanduser(),
        settings=raw,
    )
