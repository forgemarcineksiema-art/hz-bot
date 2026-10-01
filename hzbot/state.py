"""Local mirror of the game state, built from the server's partial updates."""

from __future__ import annotations

from typing import Any

# Quest status codes used by the game.
QUEST_AVAILABLE = 1
QUEST_STARTED = 2
QUEST_FINISHED = 3


def num(d: dict, *keys: str, default: float = 0) -> float:
    """First numeric value found under any of ``keys``."""
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return default


def _index(items: Any) -> dict[str, dict]:
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        return {}
    return {str(i["id"]): i for i in items if isinstance(i, dict) and "id" in i}


class GameState:
    def __init__(self) -> None:
        self.user: dict = {}
        self.character: dict = {}
        self.quests: dict[str, dict] = {}
        self.opponents: list[dict] = []
        self.work: dict = {}
        self.last_duel: dict = {}
        self.server_offset = 0.0  # server_time - local time

    def apply(self, data: dict, local_now: float) -> None:
        if not isinstance(data, dict):
            return
        if "server_time" in data:
            st = num(data, "server_time", default=-1)
            if st > 0:
                self.server_offset = st - local_now
        if isinstance(data.get("user"), dict):
            self.user.update(data["user"])
        if isinstance(data.get("character"), dict):
            self.character.update(data["character"])
        if "quests" in data:  # full list - replaces what we knew
            self.quests = _index(data["quests"])
        if isinstance(data.get("quest"), dict) and "id" in data["quest"]:
            q = data["quest"]
            self.quests.setdefault(str(q["id"]), {}).update(q)
        for key in ("opponents", "duel_opponents"):
            if isinstance(data.get(key), list):
                self.opponents = [o for o in data[key] if isinstance(o, dict)]
        if isinstance(data.get("work"), dict):
            self.work = data["work"]
        if isinstance(data.get("duel"), dict):
            self.last_duel = data["duel"]

    def server_now(self, local_now: float) -> float:
        return local_now + self.server_offset

    # --- character -------------------------------------------------------

    @property
    def character_id(self) -> str:
        return str(self.character.get("id", ""))

    @property
    def level(self) -> int:
        return int(num(self.character, "level", default=1))

    @property
    def quest_energy(self) -> float:
        return num(self.character, "quest_energy")

    @property
    def duel_stamina(self) -> float:
        return num(self.character, "duel_stamina")

    def duel_cost(self, default: int) -> float:
        return num(self.character, "duel_stamina_cost", default=default)

    @property
    def power(self) -> float:
        return combat_power(self.character)

    # --- activities ------------------------------------------------------

    @property
    def active_quest(self) -> dict | None:
        qid = str(self.character.get("active_quest_id") or "")
        if qid and qid != "0":
            return self.quests.get(qid, {"id": qid})
        return None

    @property
    def active_work(self) -> dict | None:
        wid = str(self.character.get("active_work_id") or "")
        if wid and wid != "0":
            return self.work or {"id": wid}
        return None

    def available_quests(self) -> list[dict]:
        active = self.active_quest
        active_id = str(active["id"]) if active else None
        out = []
        for qid, q in self.quests.items():
            status = int(num(q, "status", default=QUEST_AVAILABLE))
            if status == QUEST_AVAILABLE and qid != active_id:
                out.append(q)
        return out


def combat_power(c: dict) -> float:
    """Rough strength estimate: sum of total stats, falling back to level."""
    total = sum(float(v) for k, v in c.items() if k.startswith("stat_total_") and _is_num(v))
    if total:
        return total
    total = sum(float(v) for k, v in c.items() if k.startswith("stat_") and _is_num(v))
    return total or num(c, "level", default=1) * 10


def _is_num(v: Any) -> bool:
    try:
        float(v)
        return not isinstance(v, bool)
    except (TypeError, ValueError):
        return False
