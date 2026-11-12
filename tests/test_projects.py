from claude_gopher.projects import first_prose_line, list_projects, read_frontmatter


def test_frontmatter():
    fields, body = read_frontmatter("---\nsummary: Doing things.\nstate: active\n---\n# Title\nBody\n")
    assert fields == {"summary": "Doing things.", "state": "active"}
    assert body == "# Title\nBody"


def test_no_frontmatter():
    assert read_frontmatter("# Title\n") == ({}, "# Title\n")
    assert read_frontmatter("---\nunterminated\n") == ({}, "---\nunterminated\n")


def test_first_prose_line_skips_headings_and_tables():
    assert first_prose_line("# T\n\n| a |\n```\nReal line.\n") == "Real line."


def test_list_projects_joins_sessions(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "STATUS.md").write_text("---\nsummary: Alpha summary.\n---\n# Alpha\n")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "STATUS.md").write_text("# Beta\n\nBeta prose.\n")
    (tmp_path / "gamma").mkdir()
    (tmp_path / ".hidden").mkdir()
    sessions = [{"session_id": "1", "project": "alpha"}, {"session_id": "2", "project": None}]
    projects = list_projects(tmp_path, sessions)
    assert [(p["name"], p["summary"], len(p["sessions"])) for p in projects] == [
        ("alpha", "Alpha summary.", 1), ("beta", "Beta prose.", 0), ("gamma", "", 0),
    ]
