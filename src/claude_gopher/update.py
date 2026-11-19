import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from claude_gopher.config import Config

PACKAGE = "claude-gopher"
DEFAULT_SOURCE = "https://github.com/Peaches491/claude-gopher"
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
AVAILABLE = re.compile(r"v(\d+\.\d+\.\d+) is available")
LS_REMOTE_TIMEOUT = 30
INSTALL_TIMEOUT = 600
AFTER_UPDATE = [
    "gopher workon sidebar --all --restart   # sidebars run the new version",
    "gopher launch --restart --continue      # Gopher picks up the new prompt",
]


class UpdateError(Exception):
    pass


@dataclass
class UpdateStatus:
    installed: str
    latest: str | None
    editable: Path | None
    source: str

    @property
    def available(self) -> bool:
        return self.latest is not None and version_key(self.latest) > version_key(self.installed)

    def describe(self) -> str:
        if self.latest is None:
            return f"{PACKAGE} {self.installed}; no releases found at {self.source}"
        if not self.available:
            return f"{PACKAGE} {self.installed} is up to date (latest release v{self.latest})"
        text = f"{PACKAGE} v{self.latest} is available (installed {self.installed}); release notes: {self.source}/releases/tag/v{self.latest}"
        if self.editable:
            text += f"\nthis is an editable install from {self.editable}; update it with git pull there"
        return text


def update_source(config: Config) -> str:
    return config.settings.get("gopher", {}).get("update_source", DEFAULT_SOURCE).rstrip("/")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def checkout_version(directory: Path) -> str | None:
    try:
        return tomllib.loads((directory / "pyproject.toml").read_text())["project"]["version"]
    except (OSError, ValueError, KeyError):
        return None


def installed_version() -> str:
    directory = editable_dir()
    return (directory and checkout_version(directory)) or metadata.version(PACKAGE)


def reported_latest(output: str) -> str | None:
    match = AVAILABLE.search(output)
    return match.group(1) if match else None


def editable_dir() -> Path | None:
    try:
        raw = metadata.distribution(PACKAGE).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    info = json.loads(raw)
    if not info.get("dir_info", {}).get("editable"):
        return None
    return Path(info["url"].removeprefix("file://"))


def release_tags(source: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", source], capture_output=True, text=True, timeout=LS_REMOTE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise UpdateError(f"timed out listing releases at {source}") from error
    if result.returncode != 0:
        raise UpdateError(f"could not list releases at {source}: {result.stderr.strip()}")
    tags = [line.rpartition("refs/tags/")[2] for line in result.stdout.splitlines()]
    return [tag for tag in tags if RELEASE_TAG.match(tag)]


def check(config: Config) -> UpdateStatus:
    source = update_source(config)
    tags = release_tags(source)
    latest = max(tags, key=version_key).removeprefix("v") if tags else None
    return UpdateStatus(installed_version(), latest, editable_dir(), source)


def install_command(status: UpdateStatus) -> list[str]:
    return ["uv", "tool", "install", "--force", f"git+{status.source}@v{status.latest}"]


def install(status: UpdateStatus) -> None:
    if not status.available:
        raise UpdateError("nothing to install")
    if status.editable:
        raise UpdateError(f"editable install from {status.editable}; update it with git pull there")
    if not shutil.which("uv"):
        raise UpdateError("uv is not on PATH; install it, or run: " + " ".join(install_command(status)))
    try:
        result = subprocess.run(install_command(status), capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        raise UpdateError("uv tool install timed out") from error
    if result.returncode != 0:
        raise UpdateError(f"uv tool install failed: {(result.stderr or result.stdout).strip()[-500:]}")
