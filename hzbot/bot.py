"""Main game loop: quests, duels, work."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import Counter

from .client import GameError, HeroZeroClient, TransportError
from .clock import RealClock
from .config import Config
from .state import GameState, num
from .strategy import choose_opponent, choose_quest

log = logging.getLogger("hzbot")


class BotStopped(Exception):
    pass


def parse_active_hours(spec: str) -> tuple[int, int] | None:
    """``"07:00-23:30"`` -> (420, 1410) minutes of day; empty -> None."""
    spec = spec.strip()
    if not spec:
        return None
    try:
        a, b = spec.split("-")
        start = [int(x) for x in a.strip().split(":")]
        end = [int(x) for x in b.strip().split(":")]
        return start[0] * 60 + start[1], end[0] * 60 + end[1]
    except (ValueError, IndexError):
        raise ValueError(f"niepoprawne active_hours: {spec!r} (format HH:MM-HH:MM)") from None


def seconds_until_active(now: float, window: tuple[int, int] | None) -> float:
    if window is None or window[0] == window[1]:
        return 0.0
    t = time.localtime(now)
    minute = t.tm_hour * 60 + t.tm_min
    start, end = window
    inside = start <= minute < end if start <= end else (minute >= start or minute < end)
    if inside:
        return 0.0
    delta = (start - minute) % (24 * 60)
    return delta * 60 - t.tm_sec


class Bot:
    def __init__(self, cfg: Config, client: HeroZeroClient, clock=None, rng: random.Random | None = None):
        self.cfg = cfg
        self.client = client
        self.clock = clock or RealClock()
        self.rng = rng or random.Random()
        self.state = GameState()
        self.stats: Counter[str] = Counter()
        self.window = parse_active_hours(cfg.behaviour.active_hours)
        client.on_data = self._on_data
        client.dry_run = cfg.behaviour.dry_run

        self._last_sync = float("-inf")
        self._day = None
        self._duels_today = 0
        self._fought: set[str] = set()
        self._cooldown: dict[str, float] = {}
        self._started_at: dict[str, float] = {}  # quest/work id -> local start time

        # Read by the GUI from another thread.
        self.lock = threading.RLock()
        self.activity = "Uruchamianie"
        self.next_wake_at: float | None = None
        self._baseline: dict | None = None  # character values when the bot started

    # --- plumbing --------------------------------------------------------

    def _on_data(self, data: dict) -> None:
        with self.lock:
            self.state.apply(data, self.clock.now())
            c = self.state.character
            if self._baseline is None and "xp" in c:
                self._baseline = {k: num(c, k) for k in ("xp", "game_currency", "honor")}

    def snapshot(self) -> dict:
        """Thread-safe summary of the bot and character for the GUI."""
        with self.lock:
            now = self.clock.now()
            c = dict(self.state.character)

            def timer(item: dict | None, kind: str) -> dict | None:
                if item is None:
                    return None
                remaining = max(0.0, self._remaining(item, kind))
                duration = num(item, "duration") or (num(item, "hours") * 3600)
                return {"id": str(item.get("id", "")), "remaining": remaining, "duration": duration}

            return {
                "time": now,
                "activity": self.activity,
                "next_wake_in": max(0.0, self.next_wake_at - now) if self.next_wake_at else None,
                "character": {
                    "name": c.get("name", ""),
                    "level": c.get("level"),
                    "xp": c.get("xp"),
                    "coins": c.get("game_currency"),
                    "premium": c.get("premium_currency"),
                    "honor": c.get("honor"),
                    "quest_energy": c.get("quest_energy"),
                    "max_quest_energy": c.get("max_quest_energy"),
                    "duel_stamina": c.get("duel_stamina"),
                    "max_duel_stamina": c.get("max_duel_stamina"),
                },
                "quest": timer(self.state.active_quest, "quest"),
                "work": timer(self.state.active_work, "work"),
                "gained": {
                    k: num(c, k) - v for k, v in (self._baseline or {}).items()
                },
                "duels_today": self._duels_today,
                "duels_max": self.cfg.duels.max_per_day,
                "stats": dict(self.stats),
            }

    def _pause(self) -> float:
        b = self.cfg.behaviour
        return self.rng.uniform(b.min_delay, b.max_delay)

    def _act(self, name: str, params: dict | None = None, mutating: bool = True) -> dict:
        action = getattr(self.cfg.actions, name)
        data = self.client.call(action, params, mutating=mutating)
        self.stats[f"call:{name}"] += 1
        return data

    def _invalidate(self) -> None:
        self._last_sync = float("-inf")

    def _cool(self, activity: str, seconds: float) -> None:
        self._cooldown[activity] = self.clock.now() + seconds

    def _ready(self, activity: str) -> bool:
        return self.clock.now() >= self._cooldown.get(activity, 0.0)

    def sync(self) -> None:
        self._act("sync", mutating=False)
        self._last_sync = self.clock.now()

    def ensure_session(self) -> None:
        if self.client.session.logged_in:
            return
        self.client.login(self.cfg.email, self.cfg.password)
        self.client.session.save(self.cfg.session_file)

    def _relogin(self, err: GameError) -> None:
        if not (self.cfg.email and self.cfg.password and self.client.session.login_template):
            raise BotStopped(
                f"Sesja wygasła ({err.code}). Uruchom ponownie `hzbot capture` "
                "albo podaj e-mail i hasło w konfiguracji."
            ) from err
        log.warning("Sesja wygasła (%s) - loguję ponownie", err.code)
        self.client.session.user_session_id = ""
        self.ensure_session()

    # --- main loop -------------------------------------------------------

    def run(self, max_steps: int | None = None, until: float | None = None) -> Counter:
        b = self.cfg.behaviour
        start = self.clock.now()
        deadline = start + b.max_runtime_hours * 3600 if b.max_runtime_hours else None
        if until is not None:
            deadline = min(deadline, until) if deadline else until
        errors = 0
        steps = 0
        relogins = 0
        self.ensure_session()
        log.info("Start bota (dry_run=%s)", b.dry_run)
        while True:
            if deadline is not None and self.clock.now() >= deadline:
                log.info("Osiągnięto limit czasu działania")
                break
            steps += 1
            try:
                wait = self.step()
                errors = 0
                relogins = 0
            except GameError as err:
                if err.is_session_error and relogins < 2:
                    relogins += 1
                    self._relogin(err)
                    self._invalidate()
                    continue
                errors += 1
                self.stats["errors"] += 1
                log.error("Błąd gry: %s", err)
                wait = min(600.0, 15.0 * 2 ** errors)
                self._invalidate()
            except TransportError as err:
                errors += 1
                self.stats["errors"] += 1
                log.error("Błąd połączenia: %s", err)
                wait = min(900.0, 30.0 * 2 ** errors)
            if errors >= b.max_consecutive_errors:
                raise BotStopped(f"Zbyt wiele błędów z rzędu ({errors}) - zatrzymuję bota")
            if max_steps is not None and steps >= max_steps:
                break
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - self.clock.now()))
            self.next_wake_at = self.clock.now() + wait
            self.clock.sleep(wait)
        return self.stats

    def step(self) -> float:
        """Do one thing and return how many seconds to wait before the next step."""
        now = self.clock.now()
        off = seconds_until_active(now, self.window)
        if off > 0:
            self.activity = "Poza godzinami aktywności"
            log.info("Poza godzinami aktywności - pauza %.0f min", off / 60)
            return off + self.rng.uniform(30, 300)

        self._daily_reset(now)
        if now - self._last_sync >= self.cfg.behaviour.resync_minutes * 60:
            self.sync()

        quest = self.state.active_quest
        if quest is not None:
            remaining = self._remaining(quest, "quest")
            if remaining > 0:
                if self.cfg.duels.during_quest and self._can_duel():
                    return self._duel()
                self.activity = "Misja w toku"
                return remaining + self.rng.uniform(2, 15)
            return self._finish_quest(quest)

        work = self.state.active_work
        if work is not None:
            remaining = self._remaining(work, "work")
            if remaining > 0:
                self.activity = "Praca w toku"
                return remaining + self.rng.uniform(5, 60)
            return self._finish_work()

        if self._can_duel():
            return self._duel()
        if self.cfg.quests.enabled and self._ready("quest"):
            wait = self._start_quest()
            if wait is not None:
                return wait
        if self.cfg.work.enabled and self._ready("work"):
            return self._start_work()

        idle = self.cfg.behaviour.idle_poll_minutes * 60
        self.activity = "Brak energii i kondycji - czekam"
        self._invalidate()  # fetch fresh energy/stamina after the idle period
        return idle * self.rng.uniform(0.8, 1.2)

    # --- helpers ---------------------------------------------------------

    def _daily_reset(self, now: float) -> None:
        day = time.localtime(now).tm_yday
        if day != self._day:
            self._day = day
            self._duels_today = 0
            self._fought.clear()

    def _remaining(self, item: dict, kind: str) -> float:
        server_now = self.state.server_now(self.clock.now())
        end = num(item, "ts_complete", "ts_end")
        if end:
            return end - server_now
        started = self._started_at.get(f"{kind}:{item.get('id')}")
        duration = num(item, "duration")
        if started is not None and duration:
            return started + duration - self.clock.now()
        return 0.0

    # --- quests ----------------------------------------------------------

    def _start_quest(self) -> float | None:
        opt = choose_quest(self.state.available_quests(), self.state.quest_energy, self.cfg.quests)
        if opt is None:
            return None
        log.info(
            "Misja %s: %.0f min, energia %.0f, XP %.0f, monety %.0f",
            opt.id, opt.duration / 60, opt.energy, opt.xp, opt.coins,
        )
        self.activity = "Rozpoczynam misję"
        self._act("start_quest", {self.cfg.params.quest_id: opt.id})
        self.stats["quests_started"] += 1
        self._started_at[f"quest:{opt.id}"] = self.clock.now()
        if self.cfg.behaviour.dry_run:
            self._cool("quest", opt.duration)
        self._invalidate()
        return self._pause()

    def _finish_quest(self, quest: dict) -> float:
        qid = str(quest["id"])
        params = {self.cfg.params.quest_id: qid}
        if self.cfg.actions.check_quest:
            self._act("check_quest", params)
            self.clock.sleep(self._pause())
        self.activity = "Odbieram nagrodę za misję"
        self._act("claim_quest", params)
        self.stats["quests_completed"] += 1
        self._started_at.pop(f"quest:{qid}", None)
        log.info("Misja %s zakończona - nagroda odebrana", qid)
        self._invalidate()
        return self._pause()

    # --- duels -----------------------------------------------------------

    def _can_duel(self) -> bool:
        d = self.cfg.duels
        return (
            d.enabled
            and self._ready("duel")
            and self._duels_today < d.max_per_day
            and self.state.duel_stamina >= self.state.duel_cost(d.default_stamina_cost)
        )

    def _duel(self) -> float:
        if not self.state.opponents:
            self._act("get_opponents", mutating=False)
        opp = choose_opponent(self.state.opponents, self.state.power, self.cfg.duels, self._fought)
        if opp is None:
            log.info("Brak odpowiedniego przeciwnika - pojedynki wstrzymane")
            self._cool("duel", self.cfg.behaviour.idle_poll_minutes * 60)
            self.state.opponents = []
            return self._pause()
        oid = str(opp["id"])
        log.info("Pojedynek z %s (poziom %s)", opp.get("name", oid), opp.get("level", "?"))
        self.activity = f"Pojedynek z {opp.get('name', oid)}"
        self.state.last_duel = {}
        self._act("start_duel", {self.cfg.params.character_id: oid})
        if self.cfg.actions.check_duel:
            self.clock.sleep(self._pause())
            self._act("check_duel")
        self.clock.sleep(self._pause())
        self._act("claim_duel")
        self._fought.add(oid)
        self._duels_today += 1
        self.stats["duels"] += 1
        won = self.state.last_duel.get("won")
        if won is not None:
            self.stats["duels_won" if won else "duels_lost"] += 1
            log.info("Pojedynek %s", "wygrany" if won else "przegrany")
        self.state.opponents = []
        if self.cfg.behaviour.dry_run:
            self._cool("duel", self.cfg.behaviour.idle_poll_minutes * 60)
        self._invalidate()
        return self._pause()

    # --- work ------------------------------------------------------------

    def _start_work(self) -> float:
        hours = self.cfg.work.hours
        self.activity = "Idę do pracy"
        log.info("Praca na %d h", hours)
        self._act("start_work", {self.cfg.params.hours: hours})
        self.stats["work_started"] += 1
        self._cool("work", hours * 3600)
        self._invalidate()
        return self._pause()

    def _finish_work(self) -> float:
        self._act("claim_work")
        self.stats["work_completed"] += 1
        log.info("Praca zakończona - wypłata odebrana")
        self._invalidate()
        return self._pause()
