import tomllib

import pytest

from claude_gopher.config import ConfigError, gopher_features, gopher_proactivity, load_config, set_gopher_setting


def test_set_gopher_setting_inserts_and_replaces(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[paths]\nprojects_dir = "projects"\n')
    set_gopher_setting(tmp_path, "proactivity", "nudge")
    assert tomllib.loads(path.read_text())["gopher"]["proactivity"] == "nudge"
    path.write_text(path.read_text() + '\n[workon]\nproactivity = "keep"\n')
    set_gopher_setting(tmp_path, "proactivity", "quiet")
    raw = tomllib.loads(path.read_text())
    assert raw["gopher"]["proactivity"] == "quiet" and raw["workon"]["proactivity"] == "keep"
    path.write_text('[gopher]\nmodel = "opus"\n')
    set_gopher_setting(tmp_path, "proactivity", "proactive")
    assert tomllib.loads(path.read_text())["gopher"] == {"proactivity": "proactive", "model": "opus"}


def test_gopher_options_default_and_validate(tmp_path):
    (tmp_path / "config.toml").write_text("")
    config = load_config(tmp_path)
    assert gopher_proactivity(config) == "quiet" and gopher_features(config) == []
    (tmp_path / "config.toml").write_text('[gopher]\nproactivity = "loud"\nfeatures = ["diary"]\n')
    config = load_config(tmp_path)
    with pytest.raises(ConfigError):
        gopher_proactivity(config)
    with pytest.raises(ConfigError):
        gopher_features(config)
