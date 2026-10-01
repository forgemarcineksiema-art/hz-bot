import json

from hzbot.config import DuelConfig, QuestConfig
from hzbot.strategy import choose_opponent, choose_quest, parse_rewards


def q(id, energy, minutes, xp, coins, type=1, item=0):
    return {"id": id, "energy_cost": energy, "duration": minutes * 60, "type": type,
            "rewards": json.dumps({"xp": xp, "coins": coins, "item": item})}


QUESTS = [q(1, 10, 10, 100, 50), q(2, 5, 30, 80, 20), q(3, 20, 5, 150, 300, type=2)]


def test_parse_rewards_accepts_dict_string_and_garbage():
    assert parse_rewards({"xp": 1}) == {"xp": 1}
    assert parse_rewards('{"xp": 2}') == {"xp": 2}
    assert parse_rewards("not json") == {}
    assert parse_rewards(None) == {}


def test_strategies_pick_expected_quest():
    assert choose_quest(QUESTS, 100, QuestConfig(strategy="xp_per_energy")).id == "2"
    assert choose_quest(QUESTS, 100, QuestConfig(strategy="xp_per_minute")).id == "3"
    assert choose_quest(QUESTS, 100, QuestConfig(strategy="coins_per_energy")).id == "3"


def test_quest_filters():
    assert choose_quest(QUESTS, 4, QuestConfig()) is None
    assert choose_quest(QUESTS, 100, QuestConfig(strategy="xp_per_minute", avoid_fights=True)).id == "1"
    assert choose_quest(QUESTS, 100, QuestConfig(strategy="xp_per_minute", max_duration_minutes=4)) is None
    assert choose_quest(QUESTS, 12, QuestConfig(min_energy_reserve=5)).id == "2"


def test_opponent_selection():
    opps = [
        {"id": 1, "honor": 500, "stat_total_strength": 100},
        {"id": 2, "honor": 50, "stat_total_strength": 20},
        {"id": 3, "honor": 300, "stat_total_strength": 60},
    ]
    assert choose_opponent(opps, 100, DuelConfig(strategy="weakest"))["id"] == 2
    assert choose_opponent(opps, 100, DuelConfig(strategy="honor", max_power_ratio=0.9))["id"] == 3
    assert choose_opponent(opps, 10, DuelConfig(strategy="honor"))["id"] == 2
    assert choose_opponent(opps, 100, DuelConfig(), exclude={"2"})["id"] == 3
    assert choose_opponent([], 100, DuelConfig()) is None
