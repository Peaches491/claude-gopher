import io
import json

import pytest

from claude_gopher import cli
from claude_gopher.checkouts import add_checkout
from claude_gopher.config import load_config
from claude_gopher.prompt import CONTEXT_LIMIT, CONTEXTS, assemble, core_text, session_context
from claude_gopher.roots import create_project, create_root


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key in ("GOPHER_PROJECT", "GOPHER_AGENT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "root"
    create_root(path)
    monkeypatch.setenv("GOPHER_HOME", str(path))
    return path


@pytest.mark.parametrize("context", CONTEXTS)
def test_core_prompts_exist_and_are_generic(context):
    text = core_text(context).lower()
    assert text.strip()
    for word in ("zoox", "jira", "bazel", "github", "gitlab"):
        assert word not in text, (context, word)


def test_assemble_renders_and_appends_site(root):
    config = load_config(root)
    assert "## Site" not in assemble(config, "close", "alpha")
    (config.prompts_dir / "close.md").write_text("Transition tickets in {{project_dir}}.")
    text = assemble(config, "close", "alpha")
    assert str(config.closed_dir) in text
    assert text.index("# Closing") < text.index("## Site")
    assert f"Transition tickets in {config.projects_dir / 'alpha'}." in text
    with pytest.raises(ValueError):
        assemble(config, "nope")


def test_session_context_fits_limit(root):
    config = load_config(root)
    assert len(session_context(config, "project", "alpha")) < CONTEXT_LIMIT
    (config.prompts_dir / "all.md").write_text("x" * CONTEXT_LIMIT)
    text = session_context(config, "project", "alpha")
    assert len(text) == CONTEXT_LIMIT
    assert "truncated" in text


def run_hook(monkeypatch, capsys, cwd, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "cwd": cwd})))
    assert cli.main(["hook", "context"]) == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else None


def test_hook_context_by_session_kind(root, tmp_path, monkeypatch, capsys):
    config = load_config(root)
    create_project(config.projects_dir, "alpha")
    add_checkout(config.projects_dir, "alpha", tmp_path / "co")
    assert run_hook(monkeypatch, capsys, str(tmp_path / "elsewhere")) is None
    assert "# Project agent: alpha" in run_hook(monkeypatch, capsys, str(tmp_path / "co" / "src"))
    assert "# Project agent: alpha" in run_hook(monkeypatch, capsys, str(config.projects_dir / "alpha" / "notes"))
    gopher = run_hook(monkeypatch, capsys, str(config.agent_dir))
    assert "# Gopher\n" in gopher and "Project agent" not in gopher


def test_hook_context_ignores_missing_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOPHER_HOME", str(tmp_path / "absent"))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "SessionStart", "cwd": "/"}'))
    assert cli.main(["hook", "context"]) == 0
    assert capsys.readouterr().out == ""


def test_prompt_command(root, monkeypatch, capsys):
    assert cli.main(["prompt", "resources"]) == 0
    assert capsys.readouterr().out.startswith("# Resources")
    assert cli.main(["prompt", "project", "--project", "beta"]) == 0
    assert "project `beta`" in capsys.readouterr().out
