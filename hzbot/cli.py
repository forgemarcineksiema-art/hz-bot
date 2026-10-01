from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .bot import Bot, BotStopped
from .client import GameError, HeroZeroClient, TransportError
from .config import Config, load_config
from .session import Session


def _setup_logging(log_file: str | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def _load(args) -> Config:
    path = args.config
    if path is None and Path("config.yaml").exists():
        path = "config.yaml"
    return load_config(path)


def _load_session(cfg: Config) -> Session:
    path = Path(cfg.session_file)
    if not path.exists():
        raise SystemExit(f"Brak pliku sesji {path}. Uruchom najpierw: python -m hzbot capture")
    return Session.load(path)


def cmd_capture(args) -> int:
    from .capture import run_capture

    cfg = _load(args)
    url = args.url or f"https://{args.server or cfg.server}.herozerogame.com/"
    report = run_capture(url, headless=args.headless, timeout_minutes=args.timeout)
    session = report.session
    if args.salt:
        session.salt = args.salt
    previous = Path(cfg.session_file)
    if previous.exists():  # keep actions observed in earlier captures
        for action, names in Session.load(previous).observed_actions.items():
            merged = set(names) | set(session.observed_actions.get(action, []))
            session.observed_actions[action] = sorted(merged)
    session.save(cfg.session_file)
    print(f"\nPrzechwycono żądań: {report.requests}")
    print(f"Endpoint:          {session.request_url}")
    print(f"Użytkownik:        {session.user_id} (sesja {'OK' if session.logged_in else 'BRAK'})")
    print(f"Sól podpisu:       {'znaleziona' if session.salt else 'NIE ZNALEZIONA'}")
    print(f"Szablon logowania: {'tak' if report.login_captured else 'nie'}")
    print(f"Parametry stałe:   {', '.join(session.base_params) or '-'}")
    for note in report.notes:
        print(f"UWAGA: {note}")
    print(f"\nZapisano do {cfg.session_file}. Sprawdź nazwy akcji: python -m hzbot doctor")
    return 0 if session.salt and session.logged_in else 1


def cmd_doctor(args) -> int:
    cfg = _load(args)
    session = _load_session(cfg)
    ok = True
    print(f"Endpoint: {session.request_url}")
    print(f"Sesja:    {'zalogowano' if session.logged_in else 'brak'}; sól: {'jest' if session.salt else 'BRAK'}")
    if not session.salt:
        ok = False
    observed = session.observed_actions
    print("\nAkcje skonfigurowane vs. zaobserwowane w ruchu gry:")
    for name, action in asdict(cfg.actions).items():
        if not action:
            mark = "-  (wyłączona)"
        elif action in observed:
            mark = f"OK  parametry: {', '.join(observed[action]) or '-'}"
        else:
            mark = "??  nie zaobserwowano (wykonaj tę akcję w grze podczas capture, albo popraw nazwę)"
        print(f"  {name:14} {action or '':26} {mark}")
    unknown = sorted(set(observed) - set(asdict(cfg.actions).values()))
    if unknown:
        print("\nInne akcje widziane w grze (przydatne do konfiguracji):")
        for a in unknown:
            print(f"  {a:30} {', '.join(observed[a])}")
    if args.ping:
        client = HeroZeroClient(session)
        try:
            data = client.call(cfg.actions.sync, mutating=False)
        except (GameError, TransportError) as exc:
            print(f"\nPing nieudany: {exc}")
            return 1
        char = data.get("character", {})
        print(f"\nPing OK. Postać: {char.get('name', '?')} poz. {char.get('level', '?')}, "
              f"energia {char.get('quest_energy', '?')}, kondycja {char.get('duel_stamina', '?')}")
        print(f"Klucze odpowiedzi: {', '.join(sorted(data))}")
    return 0 if ok else 1


def cmd_call(args) -> int:
    cfg = _load(args)
    session = _load_session(cfg)
    params = {}
    for kv in args.params:
        k, _, v = kv.partition("=")
        params[k] = v
    data = HeroZeroClient(session).call(args.action, params, mutating=False)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_login(args) -> int:
    cfg = _load(args)
    session = _load_session(cfg)
    HeroZeroClient(session).login(cfg.email, cfg.password)
    session.save(cfg.session_file)
    print("Zalogowano, sesja zapisana.")
    return 0


def _print_stats(stats) -> None:
    if stats:
        print("\nPodsumowanie:")
        for k, v in sorted(stats.items()):
            print(f"  {k:22} {v}")


def cmd_run(args) -> int:
    cfg = _load(args)
    if args.dry_run:
        cfg.behaviour.dry_run = True
    _setup_logging(cfg.log_file, args.verbose)
    session = _load_session(cfg)
    client = HeroZeroClient(session)
    bot = Bot(cfg, client)
    try:
        bot.run(max_steps=args.steps)
    except KeyboardInterrupt:
        logging.getLogger("hzbot").info("Przerwano (Ctrl+C)")
    except BotStopped as exc:
        logging.getLogger("hzbot").error("%s", exc)
        _print_stats(bot.stats)
        return 2
    finally:
        session.save(cfg.session_file)
    _print_stats(bot.stats)
    return 0


def cmd_simulate(args) -> int:
    from .clock import VirtualClock
    from .sim import FakeServer

    cfg = _load(args) if args.config else Config()
    cfg.behaviour.dry_run = False
    _setup_logging(None, args.verbose)
    clock = VirtualClock()

    def virtual_time(record: logging.LogRecord) -> bool:
        record.created = clock.now()
        return True

    for handler in logging.getLogger().handlers:
        handler.addFilter(virtual_time)
    server = FakeServer(clock, actions=cfg.actions, seed=args.seed)
    cfg.email, cfg.password = server.email, server.password
    cfg.session_file = str(Path(args.session_out))
    client = HeroZeroClient(server.session(), server, sleep=clock.sleep)
    bot = Bot(cfg, client, clock=clock)
    stats = bot.run(until=clock.now() + args.hours * 3600)
    _print_stats(stats)
    c = server.char
    print(f"\nPostać po {args.hours} h (czasu symulowanego): XP {c['xp']}, monety {c['game_currency']}, "
          f"honor {c['honor']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hzbot", description="Bot do automatycznej gry w Hero Zero")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", help="plik YAML (domyślnie config.yaml jeśli istnieje)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("capture", help="zaloguj się w przeglądarce i przechwyć parametry protokołu")
    s.add_argument("--server", help="np. pl1, de3, us1 (nadpisuje config)")
    s.add_argument("--url", help="pełny adres strony gry (zamiast --server)")
    s.add_argument("--salt", help="ręcznie podana sól podpisu")
    s.add_argument("--headless", action="store_true")
    s.add_argument("--timeout", type=float, default=20, help="minuty oczekiwania (domyślnie 20)")
    s.set_defaults(func=cmd_capture)

    s = sub.add_parser("doctor", help="sprawdź konfigurację i nazwy akcji")
    s.add_argument("--ping", action="store_true", help="wyślij testowe zapytanie synchronizacji")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("call", help="wyślij pojedynczą akcję (debug)")
    s.add_argument("action")
    s.add_argument("params", nargs="*", metavar="klucz=wartość")
    s.set_defaults(func=cmd_call)

    s = sub.add_parser("login", help="zaloguj ponownie e-mailem i hasłem z konfiguracji")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("run", help="uruchom bota")
    s.add_argument("--dry-run", action="store_true", help="tylko pokazuj decyzje, nie wykonuj akcji")
    s.add_argument("--steps", type=int, help="zakończ po N krokach")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("simulate", help="uruchom bota na lokalnym symulatorze (bez łączenia z grą)")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--session-out", default="sim-session.json")
    s.set_defaults(func=cmd_simulate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd != "run" and args.cmd != "simulate":
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2
    except (GameError, TransportError, RuntimeError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1
