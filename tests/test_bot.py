import time

import pytest

from hzbot.bot import Bot, BotStopped, parse_active_hours, seconds_until_active
from hzbot.client import HeroZeroClient
from hzbot.clock import VirtualClock
from hzbot.config import Config
from hzbot.sim import FakeServer


def make(tmp_path, **tweaks):
    clock = VirtualClock()
    server = FakeServer(clock)
    cfg = Config(email=server.email, password=server.password, session_file=str(tmp_path / "s.json"))
    for path, value in tweaks.items():
        section, name = path.split("__")
        setattr(getattr(cfg, section), name, value)
    client = HeroZeroClient(server.session(), server, sleep=clock.sleep)
    return Bot(cfg, client, clock=clock), server, clock


def test_full_day_in_simulator(tmp_path):
    bot, server, clock = make(tmp_path)
    stats = bot.run(until=clock.now() + 24 * 3600)
    assert stats["errors"] == 0
    assert stats["quests_completed"] >= 5
    assert stats["duels"] >= 10
    assert server.char["xp"] > 0
    assert (tmp_path / "s.json").exists()  # session persisted after login


def test_respects_duel_limit_and_disabled_quests(tmp_path):
    bot, server, clock = make(tmp_path, duels__max_per_day=3, quests__enabled=False)
    stats = bot.run(until=clock.now() + 6 * 3600)
    assert stats["duels"] <= 3 * 2  # may span a midnight
    assert stats["quests_started"] == 0


def test_dry_run_sends_no_mutating_actions(tmp_path):
    bot, server, clock = make(tmp_path, behaviour__dry_run=True, work__enabled=True)
    bot.run(max_steps=50)
    mutating = {"startQuest", "claimQuestRewards", "startDuel", "claimDuelRewards", "startWork"}
    assert not mutating & set(server.log)
    assert server.char["quest_energy"] == 100


def test_relogin_after_session_expiry(tmp_path):
    bot, server, clock = make(tmp_path)
    bot.run(max_steps=5)
    server.session_id = "rotated"  # server invalidates our session
    stats = bot.run(max_steps=5)
    assert server.log.count("loginUser") == 2
    assert stats["errors"] == 0


def test_stops_on_expired_session_without_credentials(tmp_path):
    bot, server, clock = make(tmp_path)
    bot.run(max_steps=2)
    bot.cfg.password = ""
    server.session_id = "rotated"
    with pytest.raises(BotStopped):
        bot.run(max_steps=5)


def test_active_hours():
    assert parse_active_hours("") is None
    assert parse_active_hours("07:00-23:30") == (420, 1410)
    with pytest.raises(ValueError):
        parse_active_hours("7-23")
    noon = time.mktime((2026, 10, 1, 12, 0, 0, 0, 0, -1))
    assert seconds_until_active(noon, (420, 1410)) == 0
    assert seconds_until_active(noon, (13 * 60, 14 * 60)) == 3600
    assert seconds_until_active(noon, (22 * 60, 6 * 60)) == 10 * 3600  # overnight window
