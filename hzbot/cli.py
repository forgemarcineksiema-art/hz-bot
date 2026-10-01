from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .bot import Bot, BotStopped
from .client import GameError, HeroZeroClient, TransportError
from .config import Config, load_config
from .diagnostics import DISABLED, OK, check_actions
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
    import threading

    from .capture import merge_and_save, run_capture, run_capture_own_browser

    cfg = _load(args)
    url = args.url or f"https://{args.server or cfg.server}.herozerogame.com/"
    if args.bot_browser:
        report = run_capture(url, headless=args.headless, timeout_minutes=args.timeout)
    else:
        logged_in, done = threading.Event(), threading.Event()

        def on_phase(phase: str) -> None:
            if phase == "login":
                print("\nOtworzyło się okno Twojej przeglądarki. Zaloguj się do gry - captcha działa\n"
                      "normalnie, bot w tym czasie nie jest z nią połączony.\n"
                      "Gdy zobaczysz grę, wróć tutaj i naciśnij Enter.")
            elif phase == "play":
                print("\nNagrywam ruch gry. Rozpocznij i odbierz misję, stocz pojedynek,\n"
                      "potem wróć tutaj i naciśnij Enter.")

        def keyboard() -> None:
            for event in (logged_in, done):
                if not sys.stdin.readline():
                    return
                event.set()

        threading.Thread(target=keyboard, daemon=True).start()
        report = run_capture_own_browser(url, logged_in, done, timeout_minutes=args.timeout, on_phase=on_phase)
    session = merge_and_save(report, cfg.session_file, args.salt or "")
    return _print_report(report, session, cfg)


def _print_report(report, session: Session, cfg: Config) -> int:
    print(f"\nPrzechwycono żądań: {report.requests}")
    print(f"Endpoint:          {session.request_url}")
    print(f"Użytkownik:        {session.user_id} (sesja {'OK' if session.logged_in else 'BRAK'})")
    print(f"Sól podpisu:       {'znaleziona' if session.salt else 'NIE ZNALEZIONA'}")
    print(f"Szablon logowania: {'tak' if session.login_template else 'nie'}")
    print(f"Parametry stałe:   {', '.join(session.base_params) or '-'}")
    for note in report.notes:
        print(f"UWAGA: {note}")
    print(f"\nZapisano do {cfg.session_file}. Sprawdź nazwy akcji: python -m hzbot doctor")
    return 0 if session.salt and session.logged_in else 1


def cmd_import(args) -> int:
    from .capture import merge_and_save
    from .har import import_har

    cfg = _load(args)
    data = Path(args.file).read_bytes()
    report = import_har(data, fallback_page=f"https://{args.server or cfg.server}.herozerogame.com/")
    session = merge_and_save(report, cfg.session_file, args.salt or "")
    code = _print_report(report, session, cfg)
    print(f"Plik {args.file} zawiera Twoje hasło i sesję - usuń go teraz.")
    return code


def cmd_doctor(args) -> int:
    cfg = _load(args)
    session = _load_session(cfg)
    ok = True
    print(f"Endpoint: {session.request_url}")
    print(f"Sesja:    {'zalogowano' if session.logged_in else 'brak'}; sól: {'jest' if session.salt else 'BRAK'}")
    if not session.salt:
        ok = False
    rows, extra = check_actions(cfg, session)
    print("\nAkcje skonfigurowane vs. zaobserwowane w ruchu gry:")
    for r in rows:
        if r["status"] == DISABLED:
            mark = "-  (wyłączona)"
        elif r["status"] == OK:
            mark = f"OK  parametry: {', '.join(r['params']) or '-'}"
        else:
            mark = "??  nie zaobserwowano (wykonaj tę akcję w grze podczas capture, albo popraw nazwę)"
        print(f"  {r['name']:14} {r['action']:26} {mark}")
    if extra:
        print("\nInne akcje widziane w grze (przydatne do konfiguracji):")
        for e in extra:
            print(f"  {e['action']:30} {', '.join(e['params'])}")
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


def cmd_app(args) -> int:
    from .app import serve

    serve(args.config or "config.yaml", host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hzbot", description="Bot do automatycznej gry w Hero Zero")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", help="plik YAML (domyślnie config.yaml jeśli istnieje)")
    p.add_argument("-v", "--verbose", action="store_true")
    # No subcommand = open the panel.
    p.set_defaults(func=cmd_app, cmd="app", host="127.0.0.1", port=8777, no_browser=False)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("app", help="panel w przeglądarce (domyślnie)")
    s.add_argument("--host", default="127.0.0.1", help="adres nasłuchu (domyślnie tylko ten komputer)")
    s.add_argument("--port", type=int, default=8777)
    s.add_argument("--no-browser", action="store_true", help="nie otwieraj przeglądarki automatycznie")
    s.set_defaults(func=cmd_app)

    s = sub.add_parser("capture", help="zaloguj się w przeglądarce i przechwyć parametry protokołu")
    s.add_argument("--server", help="np. pl1, de3, us1 (nadpisuje config)")
    s.add_argument("--url", help="pełny adres strony gry (zamiast --server)")
    s.add_argument("--salt", help="ręcznie podana sól podpisu")
    s.add_argument("--bot-browser", action="store_true",
                   help="okno przeglądarki sterowane przez bota (captcha może nie działać)")
    s.add_argument("--headless", action="store_true", help="tylko z --bot-browser")
    s.add_argument("--timeout", type=float, default=30, help="minuty oczekiwania (domyślnie 30)")
    s.set_defaults(func=cmd_capture)

    s = sub.add_parser("import", help="połącz z grą z pliku HAR zapisanego w Twojej przeglądarce")
    s.add_argument("file", help="plik .har")
    s.add_argument("--server", help="np. pl1 (do pobrania skryptów gry, gdy HAR ich nie zawiera)")
    s.add_argument("--salt", help="ręcznie podana sól podpisu")
    s.set_defaults(func=cmd_import)

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
    if args.cmd not in ("run", "simulate", "app"):
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2
    except (GameError, TransportError, RuntimeError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1
