from pathlib import Path

from claude_gopher.checkouts import project_dirs, read_checkouts


def read_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            fields = {}
            for entry in lines[1:end]:
                key, sep, value = entry.partition(":")
                if sep and key.strip() and not key.startswith((" ", "\t")):
                    fields[key.strip()] = value.strip().strip("'\"")
            return fields, "\n".join(lines[end + 1 :])
    return {}, text


def first_prose_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "|", "```", "<!--")):
            return line
    return ""


def project_info(project_dir: Path) -> dict:
    status = project_dir / "STATUS.md"
    text = status.read_text() if status.is_file() else ""
    fields, body = read_frontmatter(text)
    return {
        "name": project_dir.name,
        "path": str(project_dir),
        "summary": fields.get("summary") or first_prose_line(body),
        "state": fields.get("state"),
        "updated": fields.get("updated"),
    }


def list_projects(projects_dir: Path, sessions: list[dict]) -> list[dict]:
    by_project = {}
    for session in sessions:
        if session.get("project"):
            by_project.setdefault(session["project"], []).append(session)
    projects = []
    for directory in project_dirs(projects_dir):
        info = project_info(directory)
        info["checkouts"] = [str(c) for c in read_checkouts(directory)]
        info["sessions"] = by_project.pop(directory.name, [])
        projects.append(info)
    return projects
