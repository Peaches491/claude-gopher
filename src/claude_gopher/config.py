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
    "reference_dir": "reference",
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
    reference_dir: Path
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


PROACTIVITY_LEVELS = ("quiet", "nudge", "proactive")
DEFAULT_PROACTIVITY = "quiet"
GOPHER_FEATURES = ("notebook", "changelog")


def gopher_proactivity(config: Config) -> str:
    level = config.settings.get("gopher", {}).get("proactivity", DEFAULT_PROACTIVITY)
    if level not in PROACTIVITY_LEVELS:
        raise ConfigError(f"[gopher] proactivity must be one of {', '.join(PROACTIVITY_LEVELS)}, not {level!r}")
    return level


def gopher_features(config: Config) -> list[str]:
    features = config.settings.get("gopher", {}).get("features", [])
    unknown = [f for f in features if f not in GOPHER_FEATURES]
    if not isinstance(features, list) or unknown:
        raise ConfigError(f"[gopher] features must be a list drawn from {', '.join(GOPHER_FEATURES)}")
    return features


DEFAULT_SUMMARY_LINES = 2
DEFAULT_SEEN_DWELL_SECONDS = 5


def seen_dwell_seconds(config: Config) -> float:
    seconds = config.settings.get("tui", {}).get("seen_dwell_seconds", DEFAULT_SEEN_DWELL_SECONDS)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        raise ConfigError(f"[tui] seen_dwell_seconds must be a non-negative number, not {seconds!r}")
    return seconds


def sidebar_summary_lines(config: Config) -> int:
    lines = config.settings.get("tui", {}).get("summary_lines", DEFAULT_SUMMARY_LINES)
    if not isinstance(lines, int) or isinstance(lines, bool) or lines < 0:
        raise ConfigError(f"[tui] summary_lines must be a whole number of lines, not {lines!r}")
    return lines


ACTION_CWDS = ("checkout", "project")


@dataclass(frozen=True)
class CustomAction:
    key: str
    label: str
    command: str
    cwd: str = "checkout"
    match: str | None = None


def tui_actions(config: Config) -> list[CustomAction]:
    raw = config.settings.get("tui", {}).get("actions", [])
    if not isinstance(raw, list):
        raise ConfigError("[[tui.actions]] must be an array of tables")
    actions = []
    for entry in raw:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry.get(k) for k in ("key", "label", "command")):
            raise ConfigError("each [[tui.actions]] needs string key, label, and command")
        cwd = entry.get("cwd", "checkout")
        if cwd not in ACTION_CWDS:
            raise ConfigError(f"[[tui.actions]] cwd must be one of {', '.join(ACTION_CWDS)}, not {cwd!r}")
        match = entry.get("match")
        if match is not None and not (isinstance(match, str) and match):
            raise ConfigError("[[tui.actions]] match must be a checkout path glob")
        actions.append(CustomAction(entry["key"], entry["label"], entry["command"], cwd, match))
    return actions


DEFAULT_SAVE_EVERY_HOURS = 2


def gopher_save_every_hours(config: Config) -> int:
    hours = config.settings.get("gopher", {}).get("save_every_hours", DEFAULT_SAVE_EVERY_HOURS)
    if not isinstance(hours, int) or isinstance(hours, bool) or not 0 <= hours <= 24:
        raise ConfigError(f"[gopher] save_every_hours must be a whole number of hours from 0 to 24, not {hours!r}")
    return hours


def set_gopher_setting(root: Path, key: str, value: str) -> None:
    path = root / ROOT_CONFIG
    lines = path.read_text().splitlines() if path.is_file() else []
    entry = f"{key} = {json.dumps(value)}"
    section = None
    header_at = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").strip()
            if section == "gopher":
                header_at = i
            continue
        if section == "gopher" and stripped.split("=", 1)[0].strip() == key and "=" in stripped:
            lines[i] = entry
            break
    else:
        if header_at is None:
            lines += ["", "[gopher]", entry] if lines else ["[gopher]", entry]
        else:
            lines.insert(header_at + 1, entry)
    path.write_text("\n".join(lines) + "\n")
    tomllib.loads(path.read_text())
