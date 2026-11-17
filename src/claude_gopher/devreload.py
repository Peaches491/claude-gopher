import hashlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from claude_gopher.config import Config

PACKAGE_DIR = Path(__file__).resolve().parent
SMOKE_TIMEOUT = 60
SMOKE_SCRIPT = """
import asyncio, sys
from pathlib import Path
from claude_gopher import tui
config = tui.load_config(Path(sys.argv[1]))
app = tui.GopherTUI(config, pinned=sys.argv[2] or None)
app.start_dev_reload = lambda: None
async def run():
    async with app.run_test(size=(85, 60)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("j", "k")
        await pilot.pause()
asyncio.run(run())
"""


class DevReloadError(Exception):
    pass


def enabled(config: Config) -> bool:
    return config.settings.get("dev", {}).get("reload") is True


def signal_path(config: Config) -> Path:
    return config.state_dir / "tui" / "reload.json"


def code_token(package_dir: Path = PACKAGE_DIR) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".md", ".vim") and "__pycache__" not in path.parts:
            digest.update(path.relative_to(package_dir).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def git_sha(package_dir: Path = PACKAGE_DIR) -> str:
    result = subprocess.run(["git", "-C", str(package_dir), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    dirty = subprocess.run(["git", "-C", str(package_dir), "status", "--porcelain", "--", "."], capture_output=True, text=True)
    return result.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")


def smoke_test(config: Config, project: str | None) -> str | None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", SMOKE_SCRIPT, str(config.root), project or ""],
            capture_output=True, text=True, timeout=SMOKE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"the TUI did not start within {SMOKE_TIMEOUT}s"
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip()[-2000:]
    return None


def read_signal(config: Config) -> dict:
    try:
        return json.loads(signal_path(config).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def publish(config: Config, project: str | None = None, smoke: bool = True) -> dict:
    if smoke and (failure := smoke_test(config, project)):
        raise DevReloadError(f"smoke test failed; sidebars keep the old code:\n{failure}")
    signal = {"token": code_token(), "sha": git_sha(), "at": time.time()}
    path = signal_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(signal) + "\n")
    temporary.replace(path)
    return signal


def watch(config: Config, on_change: Callable[[dict], None], stop: threading.Event) -> None:
    import watchfiles  # deferred: optional dev extra, only needed when [dev] reload is on

    path = signal_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    for changes in watchfiles.watch(path.parent, stop_event=stop, debounce=200, rust_timeout=1000):
        if any(Path(changed).name == path.name for _, changed in changes):
            on_change(read_signal(config))


def watcher_available() -> bool:
    try:
        import watchfiles  # noqa: F401  deferred: probing for the optional dev extra
    except ImportError:
        return False
    return True
