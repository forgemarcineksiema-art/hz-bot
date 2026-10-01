"""Decision making: which quest to take, whom to duel. Pure functions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from .config import DuelConfig, QuestConfig
from .state import combat_power, num


@dataclass
class QuestOption:
    quest: dict
    duration: float  # seconds
    energy: float
    xp: float
    coins: float
    honor: float
    has_item: bool
    is_fight: bool

    @property
    def id(self) -> str:
        return str(self.quest["id"])


def parse_rewards(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {}
        except ValueError:
            return {}
    return {}


def quest_option(q: dict) -> QuestOption:
    r = parse_rewards(q.get("rewards"))
    item = r.get("item")
    return QuestOption(
        quest=q,
        duration=max(1.0, num(q, "duration")),
        energy=num(q, "energy_cost", "quest_energy_cost"),
        xp=num(r, "xp"),
        coins=num(r, "coins", "game_currency"),
        honor=num(r, "honor"),
        has_item=bool(item) and item not in ("0", 0),
        # Fight quests are resolved by combat and can be lost.
        is_fight=int(num(q, "type", default=1)) == 2,
    )


def score(opt: QuestOption, strategy: str) -> float:
    energy = max(opt.energy, 1.0)
    minutes = opt.duration / 60.0
    if strategy == "xp_per_energy":
        return opt.xp / energy
    if strategy == "coins_per_energy":
        return opt.coins / energy
    if strategy == "xp_per_minute":
        return opt.xp / minutes
    if strategy == "coins_per_minute":
        return opt.coins / minutes
    if strategy == "balanced":
        return (opt.xp + opt.coins * 0.5 + opt.honor) / energy
    raise ValueError(f"nieznana strategia misji: {strategy}")


def choose_quest(quests: Iterable[dict], energy: float, cfg: QuestConfig) -> QuestOption | None:
    budget = energy - cfg.min_energy_reserve
    best: tuple[float, QuestOption] | None = None
    for q in quests:
        opt = quest_option(q)
        if opt.energy > budget:
            continue
        if cfg.max_duration_minutes and opt.duration > cfg.max_duration_minutes * 60:
            continue
        if cfg.avoid_fights and opt.is_fight:
            continue
        s = score(opt, cfg.strategy)
        if cfg.prefer_items and opt.has_item:
            s *= 1.5
        if best is None or s > best[0]:
            best = (s, opt)
    return best[1] if best else None


def choose_opponent(
    opponents: Iterable[dict], own_power: float, cfg: DuelConfig, exclude: set[str] = frozenset()
) -> dict | None:
    pool = [o for o in opponents if "id" in o and str(o["id"]) not in exclude]
    if not pool:
        return None
    weakest = min(pool, key=combat_power)
    if cfg.strategy == "weakest":
        return weakest
    if cfg.strategy == "honor":
        safe = [o for o in pool if combat_power(o) <= own_power * cfg.max_power_ratio]
        if not safe:
            return weakest
        return max(safe, key=lambda o: num(o, "honor"))
    raise ValueError(f"nieznana strategia pojedynków: {cfg.strategy}")
