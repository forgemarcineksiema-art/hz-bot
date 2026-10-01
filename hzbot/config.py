"""Bot configuration (YAML) with defaults for every option."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Actions:
    """Server action names. Verify them with ``hzbot doctor`` after ``hzbot capture``."""

    login: str = "loginUser"
    sync: str = "syncGame"
    start_quest: str = "startQuest"
    check_quest: str = "checkForQuestComplete"
    claim_quest: str = "claimQuestRewards"
    get_opponents: str = "getDuelOpponents"
    start_duel: str = "startDuel"
    check_duel: str = ""  # empty = step not needed
    claim_duel: str = "claimDuelRewards"
    start_work: str = "startWork"
    claim_work: str = "claimWorkReward"


@dataclass
class ParamNames:
    quest_id: str = "quest_id"
    character_id: str = "character_id"
    hours: str = "hours"


@dataclass
class Behaviour:
    dry_run: bool = False
    min_delay: float = 1.5  # seconds between consecutive actions (randomised)
    max_delay: float = 4.0
    active_hours: str = ""  # e.g. "07:00-23:30"; empty = always
    idle_poll_minutes: float = 15.0
    resync_minutes: float = 10.0
    max_runtime_hours: float = 0.0  # 0 = unlimited
    max_consecutive_errors: int = 5


@dataclass
class QuestConfig:
    enabled: bool = True
    # xp_per_energy | coins_per_energy | xp_per_minute | coins_per_minute | balanced
    strategy: str = "xp_per_energy"
    min_energy_reserve: int = 0
    max_duration_minutes: float = 0.0  # 0 = unlimited
    prefer_items: bool = False
    avoid_fights: bool = False


@dataclass
class DuelConfig:
    enabled: bool = True
    strategy: str = "weakest"  # weakest | honor
    max_power_ratio: float = 0.9  # "honor": only opponents weaker than ratio * own power
    max_per_day: int = 50
    during_quest: bool = False
    default_stamina_cost: int = 20


@dataclass
class WorkConfig:
    enabled: bool = False
    hours: int = 1  # work blocks quests, so it only starts when nothing else is possible


@dataclass
class Config:
    server: str = "pl1"
    session_file: str = "session.json"
    log_file: str = "hzbot.log"
    email: str = ""
    password: str = ""
    behaviour: Behaviour = field(default_factory=Behaviour)
    quests: QuestConfig = field(default_factory=QuestConfig)
    duels: DuelConfig = field(default_factory=DuelConfig)
    work: WorkConfig = field(default_factory=WorkConfig)
    actions: Actions = field(default_factory=Actions)
    params: ParamNames = field(default_factory=ParamNames)


def _build(cls: type, data: dict[str, Any] | None, path: str) -> Any:
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: oczekiwano słownika")
    defaults = cls()
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{path}: nieznane opcje: {', '.join(sorted(unknown))}")
    kwargs = {}
    for name, value in data.items():
        default = getattr(defaults, name)
        if is_dataclass(default):
            value = _build(type(default), value, f"{path}.{name}")
        elif value is None:
            value = default
        elif isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        elif not isinstance(value, type(default)):
            raise ValueError(f"{path}.{name}: oczekiwano typu {type(default).__name__}")
        kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> Config:
    raw: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        else:
            raise FileNotFoundError(f"Brak pliku konfiguracji: {p}")
    cfg = _build(Config, raw, "config")
    cfg.email = os.environ.get("HZ_EMAIL", cfg.email)
    cfg.password = os.environ.get("HZ_PASSWORD", cfg.password)
    return cfg


def config_from_dict(data: dict[str, Any]) -> Config:
    """Validate a raw (YAML-shaped) dictionary. Raises ValueError with a readable path."""
    return _build(Config, data, "config")


def read_raw_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: oczekiwano słownika YAML")
    return data


def write_raw_config(path: str | Path, data: dict[str, Any]) -> None:
    config_from_dict(data)  # never write an invalid file
    p = Path(path)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if data.get("password"):
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
