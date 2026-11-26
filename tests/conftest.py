import pytest

from claude_gopher import schedule


@pytest.fixture(autouse=True)
def no_gh_by_default(monkeypatch):
    monkeypatch.setattr(schedule, "have_gh", lambda: False)
