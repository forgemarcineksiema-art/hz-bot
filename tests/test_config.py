import pytest

from hzbot.config import load_config


def test_defaults_without_file():
    cfg = load_config(None)
    assert cfg.quests.enabled and cfg.actions.start_quest == "startQuest"


def test_yaml_overrides_and_validation(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("server: de3\nbehaviour:\n  min_delay: 2\nactions:\n  sync: refreshGame\n")
    monkeypatch.setenv("HZ_PASSWORD", "pw")
    cfg = load_config(p)
    assert cfg.server == "de3"
    assert cfg.behaviour.min_delay == 2.0
    assert cfg.actions.sync == "refreshGame"
    assert cfg.password == "pw"

    p.write_text("quests:\n  bogus: 1\n")
    with pytest.raises(ValueError, match="bogus"):
        load_config(p)
    p.write_text("quests:\n  enabled: 'yes'\n")
    with pytest.raises(ValueError):
        load_config(p)
