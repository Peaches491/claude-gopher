import json
import tomllib
from pathlib import Path

CHECKOUTS_FILE = "checkouts.toml"


def checkouts_file(project_dir: Path) -> Path:
    return project_dir / CHECKOUTS_FILE


def read_checkouts(project_dir: Path) -> list[Path]:
    path = checkouts_file(project_dir)
    if not path.is_file():
        return []
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        return []
    return [Path(entry["path"]).expanduser() for entry in raw.get("checkout", []) if entry.get("path")]


def write_checkouts(project_dir: Path, checkouts: list[Path]) -> None:
    home = Path.home()
    blocks = []
    for checkout in checkouts:
        shown = f"~/{checkout.relative_to(home)}" if home in checkout.parents else str(checkout)
        blocks.append(f"[[checkout]]\npath = {json.dumps(shown)}\n")
    checkouts_file(project_dir).write_text("\n".join(blocks))


def project_dirs(projects_dir: Path) -> list[Path]:
    if not projects_dir.is_dir():
        return []
    return sorted(p for p in projects_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def all_checkouts(projects_dir: Path) -> list[tuple[str, Path]]:
    return [(d.name, checkout) for d in project_dirs(projects_dir) for checkout in read_checkouts(d)]


def owners(projects_dir: Path, checkout: Path) -> list[str]:
    return [name for name, path in all_checkouts(projects_dir) if path == checkout]


def project_for_cwd(projects_dir: Path, cwd: str) -> str | None:
    if not cwd:
        return None
    path = Path(cwd)
    best = None
    for name, checkout in all_checkouts(projects_dir):
        if path == checkout or checkout in path.parents:
            if best is None or len(checkout.parts) > len(best[1].parts):
                best = (name, checkout)
    return best[0] if best else None


def add_checkout(projects_dir: Path, project: str, checkout: Path) -> None:
    project_dir = projects_dir / project
    if not project_dir.is_dir():
        raise FileNotFoundError(f"no project {project!r} in {projects_dir}")
    current = read_checkouts(project_dir)
    if checkout not in current:
        write_checkouts(project_dir, [*current, checkout])


def remove_checkout(projects_dir: Path, project: str, checkout: Path) -> bool:
    project_dir = projects_dir / project
    current = read_checkouts(project_dir)
    if checkout not in current:
        return False
    write_checkouts(project_dir, [c for c in current if c != checkout])
    return True
