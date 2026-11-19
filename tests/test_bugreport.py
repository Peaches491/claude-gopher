import json
from pathlib import Path

import pytest

from claude_gopher import bugreport, doctor
from claude_gopher.cli import main
from claude_gopher.roots import create_root


def test_draft_redacts_home_and_lists_environment():
    home = Path("/home/someone")
    body = bugreport.draft(
        "sidebar crashes", "It crashed in /home/someone/work.", ["fail  config    /home/someone/work/config.toml broken"],
        [("claude-gopher", "0.1.3"), ("tmux", "tmux 3.4")], home,
    )
    assert "/home/someone" not in body
    assert "It crashed in ~/work." in body and "~/work/config.toml broken" in body
    assert "- claude-gopher: 0.1.3" in body and body.count("```") == 2
    assert bugreport.redact("/home/someonex/a", home) == "/home/someonex/a"


def test_search_terms_drop_filler_words():
    assert bugreport.search_terms("The sidebar does not show the schedule!") == "sidebar show schedule"
    assert bugreport.search_terms("the a an") == ""


@pytest.fixture
def fake_gh(monkeypatch):
    calls = []

    def gh(*argv, input=None):
        calls.append((argv, input))
        if argv[:2] == ("issue", "list"):
            return json.dumps([{"number": 7, "title": "Schedule window vanishes", "state": "OPEN", "url": "https://x/7"}])
        if argv[:2] == ("issue", "create"):
            return "https://x/8\n"
        if argv[:2] == ("issue", "comment"):
            return "https://x/7#c\n"
        raise AssertionError(argv)

    monkeypatch.setattr(bugreport, "gh", gh)
    monkeypatch.setattr(bugreport, "environment", lambda: [("claude-gopher", "0.1.3")])
    monkeypatch.setattr(doctor, "run", lambda config: [doctor.Check(doctor.OK, "install", "fine")])
    return calls


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "root"
    create_root(root)
    return root


def test_report_without_yes_drafts_and_searches_but_posts_nothing(root, fake_gh, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["--root", str(root), "doctor", "--report", "schedule window vanishes", "--description", "gone"]) == 0
    out = capsys.readouterr().out
    assert "Title: schedule window vanishes" in out and "#7 [open] Schedule window vanishes" in out
    assert "Nothing posted" in out
    assert [argv[:2] for argv, _ in fake_gh] == [("issue", "list")]
    assert "schedule window vanishes in:title,body" in fake_gh[0][0]


def test_report_yes_files_and_comment_adds(root, fake_gh, capsys):
    assert main(["--root", str(root), "doctor", "--report", "it broke", "--yes"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("https://x/8")
    argv, body = fake_gh[-1]
    assert argv[:2] == ("issue", "create") and "--title" in argv and "### gopher doctor" in body
    assert main(["--root", str(root), "doctor", "--report", "it broke", "--comment", "7"]) == 0
    assert fake_gh[-1][0][:3] == ("issue", "comment", "7")
    assert ("issue", "list") not in [argv[:2] for argv, _ in fake_gh[2:]]


def test_report_without_gh_points_at_the_web_form(root, monkeypatch, capsys):
    monkeypatch.setattr(bugreport.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor, "run", lambda config: [])
    assert main(["--root", str(root), "doctor", "--report", "it broke", "--yes"]) == 1
    assert bugreport.NEW_ISSUE_URL in capsys.readouterr().err
