"""Offline game server simulator speaking the same protocol as ``request.php``.

Used by the tests and by ``hzbot simulate`` to exercise the bot without
touching the real game.
"""

from __future__ import annotations

import json
import random
import time

from .auth import make_auth
from .clock import VirtualClock
from .config import Actions
from .session import Session

SIM_URL = "https://sim.local/request.php"


class FakeServer:
    def __init__(self, clock: VirtualClock, *, salt: str = "sim-salt", seed: int = 1,
                 actions: Actions | None = None):
        self.clock = clock
        self.salt = salt
        self.rng = random.Random(seed)
        self.user_id = "1001"
        self.session_id = ""
        self.email = "hero@example.com"
        self.password = "secret"
        self.next_id = 1
        self.log: list[str] = []
        self.char = {
            "id": 5001, "name": "SimHero", "level": 10, "xp": 0, "game_currency": 0, "honor": 100,
            "quest_energy": 100, "max_quest_energy": 100,
            "duel_stamina": 100, "max_duel_stamina": 100, "duel_stamina_cost": 20,
            "active_quest_id": 0, "active_work_id": 0,
            "stat_total_strength": 40, "stat_total_stamina": 40,
            "stat_total_critical_rating": 30, "stat_total_dodge_rating": 30,
        }
        self.quests: dict[int, dict] = {}
        self.work: dict = {}
        self.pending_duel: dict | None = None
        self._last_regen = clock.now()
        self._day = time.localtime(clock.now()).tm_yday
        self._refill_quests()

        a = actions or Actions()
        self.login_action = a.login
        self.handlers = {
            a.login: self._login, a.sync: self._sync,
            a.start_quest: self._start_quest, a.check_quest: self._check_quest,
            a.claim_quest: self._claim_quest, a.get_opponents: self._opponents,
            a.start_duel: self._start_duel, a.claim_duel: self._claim_duel,
            a.start_work: self._start_work, a.claim_work: self._claim_work,
        }

    def session(self) -> Session:
        """Session as `hzbot capture` would have produced it (logged out)."""
        return Session(
            request_url=SIM_URL,
            salt=self.salt,
            base_params={"client_version": "sim_1"},
            login_template={
                "action": "loginUser", "user_id": "0", "user_session_id": "0",
                "email": "{email}", "password": "{password}", "client_version": "sim_1",
            },
        )

    # --- transport entry point -------------------------------------------

    def __call__(self, url: str, form: dict[str, str]) -> dict:
        action = form.get("action", "")
        self.log.append(action)
        if form.get("auth") != make_auth(action, form.get("user_id", ""), self.salt):
            return {"error": "errInvalidAuth", "data": {}}
        handler = self.handlers.get(action)
        if handler is None:
            return {"error": "errUnknownAction", "data": {}}
        if action != self.login_action and (
            form.get("user_id") != self.user_id or not self.session_id
            or form.get("user_session_id") != self.session_id
        ):
            return {"error": "errUserNotAuthorized", "data": {}}
        self._tick()
        try:
            data = handler(form)
        except _Err as e:
            return {"error": str(e), "data": {}}
        data["server_time"] = int(self.clock.now())
        return {"error": "", "data": data}

    # --- world simulation ------------------------------------------------

    def _tick(self) -> None:
        now = self.clock.now()
        day = time.localtime(now).tm_yday
        if day != self._day:  # daily quest energy refill
            self._day = day
            self.char["quest_energy"] = self.char["max_quest_energy"]
        regen = int((now - self._last_regen) // 180)  # 1 duel stamina per 3 min
        if regen:
            self._last_regen += regen * 180
            self.char["duel_stamina"] = min(self.char["max_duel_stamina"], self.char["duel_stamina"] + regen)

    def _refill_quests(self) -> None:
        for q in [q for q in self.quests.values() if q["status"] == 1]:
            del self.quests[q["id"]]
        for _ in range(3):
            qid = self._id()
            minutes = self.rng.choice([1, 2, 5, 10, 15, 20, 30])
            energy = minutes  # in Hero Zero energy cost is proportional to duration
            self.quests[qid] = {
                "id": qid, "character_id": self.char["id"], "stage": 1, "status": 1,
                "type": self.rng.choice([1, 1, 2]), "duration": minutes * 60, "energy_cost": energy,
                "rewards": json.dumps({
                    "coins": int(energy * self.rng.uniform(5, 15)),
                    "xp": int(energy * self.rng.uniform(4, 12)),
                    "honor": 0, "item": self.rng.random() < 0.1 and 1 or 0,
                }),
            }

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _state(self) -> dict:
        return {"character": dict(self.char), "quests": [dict(q) for q in self.quests.values()],
                **({"work": dict(self.work)} if self.work else {})}

    # --- handlers --------------------------------------------------------

    def _login(self, f: dict) -> dict:
        if f.get("email") != self.email or f.get("password") != self.password:
            raise _Err("errLoginWrongCredentials")
        self.session_id = f"sess{self._id()}"
        return {"user": {"id": self.user_id, "session_id": self.session_id}, **self._state()}

    def _sync(self, f: dict) -> dict:
        return self._state()

    def _quest(self, f: dict) -> dict:
        q = self.quests.get(int(f.get("quest_id", 0)))
        if q is None:
            raise _Err("errQuestNotFound")
        return q

    def _start_quest(self, f: dict) -> dict:
        q = self._quest(f)
        if self.char["active_quest_id"] or self.char["active_work_id"]:
            raise _Err("errCharacterBusy")
        if q["status"] != 1:
            raise _Err("errQuestNotAvailable")
        if self.char["quest_energy"] < q["energy_cost"]:
            raise _Err("errNotEnoughQuestEnergy")
        self.char["quest_energy"] -= q["energy_cost"]
        self.char["active_quest_id"] = q["id"]
        q["status"] = 2
        q["ts_complete"] = int(self.clock.now()) + q["duration"]
        return {"character": dict(self.char), "quest": dict(q)}

    def _check_quest(self, f: dict) -> dict:
        q = self._quest(f)
        if q["status"] == 2 and self.clock.now() >= q["ts_complete"]:
            q["status"] = 3
        if q["status"] != 3:
            raise _Err("errQuestNotComplete")
        return {"quest": dict(q)}

    def _claim_quest(self, f: dict) -> dict:
        q = self._quest(f)
        if q["status"] != 3:
            raise _Err("errQuestNotComplete")
        r = json.loads(q["rewards"])
        self.char["xp"] += r["xp"]
        self.char["game_currency"] += r["coins"]
        self.char["active_quest_id"] = 0
        del self.quests[q["id"]]
        self._refill_quests()
        return self._state()

    def _opponents(self, f: dict) -> dict:
        opps = []
        for _ in range(5):
            power = self.rng.randint(60, 220)
            opps.append({
                "id": self._id(), "name": f"Rywal{self.next_id}", "level": self.rng.randint(8, 12),
                "honor": self.rng.randint(50, 300),
                "stat_total_strength": power // 2, "stat_total_stamina": power - power // 2,
            })
        self._opps = {o["id"]: o for o in opps}
        return {"opponents": opps}

    def _start_duel(self, f: dict) -> dict:
        opp = getattr(self, "_opps", {}).get(int(f.get("character_id", 0)))
        if opp is None:
            raise _Err("errOpponentNotFound")
        if self.char["duel_stamina"] < self.char["duel_stamina_cost"]:
            raise _Err("errNotEnoughDuelStamina")
        if self.pending_duel:
            raise _Err("errDuelAlreadyRunning")
        self.char["duel_stamina"] -= self.char["duel_stamina_cost"]
        mine = 140
        theirs = opp["stat_total_strength"] + opp["stat_total_stamina"]
        won = self.rng.random() < mine / (mine + theirs)
        self.pending_duel = {"id": self._id(), "won": won, "honor": 10 if won else -5}
        return {"character": dict(self.char), "duel": dict(self.pending_duel)}

    def _claim_duel(self, f: dict) -> dict:
        if not self.pending_duel:
            raise _Err("errNoDuel")
        self.char["honor"] += self.pending_duel["honor"]
        duel, self.pending_duel = self.pending_duel, None
        return {"character": dict(self.char), "duel": duel}

    def _start_work(self, f: dict) -> dict:
        if self.char["active_quest_id"] or self.char["active_work_id"]:
            raise _Err("errCharacterBusy")
        hours = int(f.get("hours", 1))
        self.work = {"id": self._id(), "hours": hours, "ts_complete": int(self.clock.now()) + hours * 3600}
        self.char["active_work_id"] = self.work["id"]
        return self._state()

    def _claim_work(self, f: dict) -> dict:
        if not self.work or self.clock.now() < self.work["ts_complete"]:
            raise _Err("errWorkNotComplete")
        self.char["game_currency"] += 50 * self.work["hours"]
        self.char["active_work_id"] = 0
        self.work = {}
        return {"character": dict(self.char), "work": {}}


class _Err(Exception):
    pass
