import json
import threading
import time

import pytest

from claude_gopher import devreload
from claude_gopher.config import load_config
from claude_gopher.roots import create_root


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for key, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "root"
    create_root(root)
    return load_config(root)


def test_enabled_only_when_opted_in(config):
    assert not devreload.enabled(config)
    path = config.root / "config.toml"
    path.write_text(path.read_text() + "\n[dev]\nreload = true\n")
    assert devreload.enabled(load_config(config.root))


def test_code_token_follows_content(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    first = devreload.code_token(tmp_path)
    assert devreload.code_token(tmp_path) == first
    (tmp_path / "a.py").write_text("x = 2\n")
    assert devreload.code_token(tmp_path) != first


def test_publish_writes_signal_unless_smoke_fails(config, monkeypatch):
    monkeypatch.setattr(devreload, "smoke_test", lambda config, project: "Traceback: boom")
    with pytest.raises(devreload.DevReloadError, match="boom"):
        devreload.publish(config)
    assert devreload.read_signal(config) == {}
    monkeypatch.setattr(devreload, "smoke_test", lambda config, project: None)
    signal = devreload.publish(config)
    assert json.loads(devreload.signal_path(config).read_text())["token"] == signal["token"] == devreload.code_token()


def test_smoke_test_starts_the_real_tui(config):
    path = config.root / "config.toml"
    path.write_text(path.read_text() + "\n[dev]\nreload = true\n")
    assert devreload.smoke_test(load_config(config.root), None) is None


def test_watch_reports_signal_changes(config):
    pytest.importorskip("watchfiles")
    seen, stop = [], threading.Event()

    def changed(signal):
        seen.append(signal)
        stop.set()

    thread = threading.Thread(target=devreload.watch, args=(config, changed, stop), daemon=True)
    thread.start()
    time.sleep(0.5)
    devreload.publish(config, smoke=False)
    thread.join(timeout=10)
    assert seen and seen[0]["token"] == devreload.code_token()
