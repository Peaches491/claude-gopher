import copy
import json
import shutil
import sys
import time
from pathlib import Path

from claude_gopher.state import HOOK_EVENTS

HOOKS = {"state": HOOK_EVENTS, "context": {"SessionStart": None}, "inbox": {"UserPromptSubmit": None, "Stop": None}}


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def gopher_command() -> str:
    found = shutil.which("gopher")
    return found or str(Path(sys.argv[0]).resolve())


def is_gopher_hook(command: str, name: str) -> bool:
    parts = command.split()
    return len(parts) >= 3 and Path(parts[-3]).name == "gopher" and parts[-2:] == ["hook", name]


def has_hook(entries: list[dict], name: str, matcher: str | None) -> bool:
    return any(
        entry.get("matcher") == matcher and any(is_gopher_hook(h.get("command", ""), name) for h in entry.get("hooks", []))
        for entry in entries
    )


def plan_hooks(settings: dict, binary: str) -> tuple[dict, list[str]]:
    updated = copy.deepcopy(settings)
    hooks = updated.setdefault("hooks", {})
    added = []
    for name, events in HOOKS.items():
        for event, matcher in events.items():
            entries = hooks.setdefault(event, [])
            if has_hook(entries, name, matcher):
                continue
            entry = {"hooks": [{"type": "command", "command": f"{binary} hook {name}"}]}
            if matcher:
                entry["matcher"] = matcher
            entries.append(entry)
            added.append(f"{event}{f' [{matcher}]' if matcher else ''}: {binary} hook {name}")
    return updated, added


def read_settings(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def write_settings(path: Path, settings: dict) -> Path | None:
    backup = None
    if path.is_file():
        backup = path.with_name(f"{path.name}.bak-gopher-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".gopher-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(path)
    return backup
