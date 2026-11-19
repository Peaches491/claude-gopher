import json
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_gopher import update

REPO = update.DEFAULT_SOURCE.removeprefix("https://github.com/")
NEW_ISSUE_URL = f"{update.DEFAULT_SOURCE}/issues/new?template=bug_report.yml"
GH_TIMEOUT = 60
SEARCH_LIMIT = 5
STOP_WORDS = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "is", "it", "when", "with", "for", "not", "does", "doesn't"}


class BugReportError(Exception):
    pass


@dataclass
class Match:
    number: int
    title: str
    state: str
    url: str


def redact(text: str, home: Path | None = None) -> str:
    home = home or Path.home()
    return re.sub(re.escape(str(home)) + r"(?=/|\b)", "~", text)


def tool_version(*argv: str) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "not found"
    return (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else "not found"


def environment() -> list[tuple[str, str]]:
    try:
        version = update.installed_version()
    except Exception as error:
        version = f"unknown ({error})"
    return [
        ("claude-gopher", version),
        ("Claude Code", tool_version("claude", "--version")),
        ("tmux", tool_version("tmux", "-V")),
        ("Python", platform.python_version()),
        ("OS", f"{platform.system()} {platform.release()} ({platform.machine()})"),
    ]


def draft(title: str, description: str, doctor_lines: list[str], env: list[tuple[str, str]], home: Path | None = None) -> str:
    body = [
        "### What happened",
        "",
        description.strip() or "_No description given._",
        "",
        "### Environment",
        "",
        *(f"- {name}: {value}" for name, value in env),
        "",
        "### gopher doctor",
        "",
        "```",
        *doctor_lines,
        "```",
        "",
        "_Filed with `gopher doctor --report`._",
    ]
    return redact("\n".join(body) + "\n", home)


def gh(*argv: str, input: str | None = None) -> str:
    if not shutil.which("gh"):
        raise BugReportError(f"the GitHub CLI (gh) is not installed; file it by hand at {NEW_ISSUE_URL}")
    try:
        result = subprocess.run(["gh", *argv], input=input, capture_output=True, text=True, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BugReportError(f"gh {argv[0]} timed out; file it by hand at {NEW_ISSUE_URL}") from None
    if result.returncode != 0:
        raise BugReportError(f"gh {' '.join(argv[:2])} failed: {result.stderr.strip()}; file it by hand at {NEW_ISSUE_URL}")
    return result.stdout


def search_terms(title: str) -> str:
    words = [word for word in re.findall(r"[\w-]+", title.lower()) if word not in STOP_WORDS]
    return " ".join(words)


def search(title: str) -> list[Match]:
    terms = search_terms(title)
    if not terms:
        return []
    raw = gh("issue", "list", "-R", REPO, "--state", "all", "--search", f"{terms} in:title,body",
             "--limit", str(SEARCH_LIMIT), "--json", "number,title,state,url")
    return [Match(item["number"], item["title"], item["state"].lower(), item["url"]) for item in json.loads(raw or "[]")]


def file_issue(title: str, body: str) -> str:
    return gh("issue", "create", "-R", REPO, "--title", title, "--body-file", "-", input=body).strip()


def comment(number: int, body: str) -> str:
    return gh("issue", "comment", str(number), "-R", REPO, "--body-file", "-", input=body).strip()
