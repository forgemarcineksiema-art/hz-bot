"""Local web panel: start/stop the bot, watch progress, edit settings.

Runs a small HTTP server on 127.0.0.1 (standard library only) and serves a
single-page UI from ``hzbot/web/index.html``.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .bot import Bot, BotStopped
from .client import GameError, HeroZeroClient, TransportError
from .clock import ControlledClock, StopRequested
from .config import (
    Config,
    config_from_dict,
    load_config,
    read_raw_config,
    write_raw_config,
)
from .diagnostics import check_actions
from .session import Session

log = logging.getLogger("hzbot")
WEB_DIR = Path(__file__).parent / "web"
SIM_SPEEDS = (1, 60, 300, 1200)
MAX_UPLOAD = 600 * 1024 * 1024  # HAR files with game assets can be big


class RingLog(logging.Handler):
    """Keeps the last log lines in memory for the UI."""

    def __init__(self, size: int = 1000):
        super().__init__(logging.INFO)
        self.lines: deque[dict] = deque(maxlen=size)
        self.next_id = 1
        self.time_source = None  # callable -> float; virtual time during simulation
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        t = self.time_source() if self.time_source else record.created
        with self._lock:
            self.lines.append({"id": self.next_id, "t": t, "level": record.levelname, "msg": msg})
            self.next_id += 1

    def since(self, after: int) -> list[dict]:
        with self._lock:
            return [line for line in self.lines if line["id"] > after]


class Controller:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.logs = RingLog()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.bot: Bot | None = None
        self.mode = "stopped"  # stopped | running | capture
        self.run_kind: str | None = None  # game | dry | sim
        self.speed = 1
        self.started_at: float | None = None
        self.error: str | None = None
        self.last_snapshot: dict | None = None
        self._capture_done = threading.Event()
        self._capture_login = threading.Event()
        self.browser_args: tuple[str, ...] = ()  # extra flags for the user's browser (tests)
        self.capture_result: dict | None = None

    # --- config ----------------------------------------------------------

    def config(self) -> Config:
        path = Path(self.config_path)
        return load_config(path if path.exists() else None)

    def config_for_ui(self) -> dict:
        raw = read_raw_config(self.config_path)
        data = asdict(config_from_dict(raw))
        data["password"] = ""
        return {
            "config": data,
            "password_set": bool(raw.get("password")),
            "env_credentials": bool(self.config().password and not raw.get("password")),
            "path": str(Path(self.config_path).resolve()),
        }

    def save_config(self, data: dict) -> None:
        raw = read_raw_config(self.config_path)
        data = dict(data)
        if not data.get("password"):  # empty field = keep the current password
            data.pop("password", None)
            if raw.get("password"):
                data["password"] = raw["password"]
        write_raw_config(self.config_path, data)
        log.info("Zapisano ustawienia")

    # --- status ----------------------------------------------------------

    def setup_state(self) -> dict:
        cfg = self.config()
        out = {
            "server": cfg.server,
            "config_exists": Path(self.config_path).exists(),
            "session_exists": Path(cfg.session_file).exists(),
            "logged_in": False, "salt": False, "login_template": False,
            "credentials": bool(cfg.email and cfg.password),
        }
        if out["session_exists"]:
            try:
                s = Session.load(cfg.session_file)
                out.update(logged_in=s.logged_in, salt=bool(s.salt), login_template=bool(s.login_template))
            except (ValueError, TypeError, OSError):
                pass
        out["ready"] = out["salt"] and (out["logged_in"] or (out["login_template"] and out["credentials"]))
        return out

    def status(self) -> dict:
        bot = self.bot
        if bot is not None and self.mode == "running":
            self.last_snapshot = bot.snapshot()
        return {
            "mode": self.mode,
            "run_kind": self.run_kind,
            "speed": self.speed,
            "uptime": time.time() - self.started_at if self.started_at and self.mode == "running" else None,
            "error": self.error,
            "setup": self.setup_state(),
            "capture": self.capture_result,
            "bot": self.last_snapshot,
            "log_id": self.logs.next_id - 1,
        }

    # --- bot -------------------------------------------------------------

    def start(self, kind: str, speed: int = 1) -> None:
        if kind not in ("game", "dry", "sim"):
            raise ValueError("nieznany tryb")
        with self._lock:
            if self.mode != "stopped":
                raise ValueError("Bot już działa - najpierw go zatrzymaj.")
            cfg = self.config()
            if kind != "sim" and not self.setup_state()["ready"]:
                raise ValueError("Najpierw połącz bota z grą (krok „Połącz z grą”).")
            self._stop.clear()
            self.mode, self.run_kind, self.error = "running", kind, None
            self.speed = speed if kind == "sim" and speed in SIM_SPEEDS else 1
            self.started_at = time.time()
            self.last_snapshot = None
            self._thread = threading.Thread(target=self._run, args=(cfg, kind), daemon=True)
            self._thread.start()

    def _run(self, cfg: Config, kind: str) -> None:
        session: Session | None = None
        clock = ControlledClock(self._stop, self.speed)
        try:
            if kind == "sim":
                from .sim import FakeServer

                server = FakeServer(clock, actions=cfg.actions, seed=int(time.time()))
                cfg.email, cfg.password = server.email, server.password
                cfg.session_file = str(Path(tempfile.gettempdir()) / "hzbot-sim-session.json")
                cfg.behaviour.active_hours = ""
                client = HeroZeroClient(server.session(), server, sleep=clock.sleep)
                self.logs.time_source = clock.now
                log.info("Symulacja (x%d) - bez łączenia z prawdziwą grą", self.speed)
            else:
                cfg.behaviour.dry_run = kind == "dry"
                session = Session.load(cfg.session_file)
                client = HeroZeroClient(session, sleep=clock.sleep)
            self.bot = Bot(cfg, client, clock=clock)
            self.bot.run()
            log.info("Bot zakończył pracę")
        except StopRequested:
            log.info("Zatrzymano bota")
        except (BotStopped, GameError, TransportError, RuntimeError, ValueError, OSError) as exc:
            self.error = str(exc)
            log.error("Bot zatrzymany: %s", exc)
        except Exception as exc:  # keep the panel alive whatever happens
            self.error = f"Nieoczekiwany błąd: {exc}"
            log.exception("Nieoczekiwany błąd")
        finally:
            if self.bot is not None:
                self.last_snapshot = self.bot.snapshot()
            if session is not None:
                try:
                    session.save(cfg.session_file)
                except OSError:
                    pass
            self.logs.time_source = None
            self.bot = None
            self.mode = "stopped"

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    # --- capture / diagnostics ---------------------------------------------

    def start_capture(self, method: str = "own") -> None:
        """method: "own" = user's Chrome/Edge (captcha-friendly), "bot" = Playwright window."""
        if method not in ("own", "bot"):
            raise ValueError("nieznana metoda łączenia")
        with self._lock:
            if self.mode != "stopped":
                raise ValueError("Zatrzymaj bota przed łączeniem z grą.")
            self.mode, self.error = "capture", None
            self.capture_result = {"active": True, "method": method,
                                   "phase": "starting" if method == "own" else "play"}
            self._capture_login.clear()
            self._capture_done.clear()
            threading.Thread(target=self._capture, args=(self.config(), method), daemon=True).start()

    def _set_phase(self, phase: str) -> None:
        if self.capture_result and self.capture_result.get("active"):
            self.capture_result = {**self.capture_result, "phase": phase}

    def _capture(self, cfg: Config, method: str) -> None:
        from .capture import merge_and_save, run_capture, run_capture_own_browser

        try:
            url = f"https://{cfg.server}.herozerogame.com/"
            log.info("Otwieram grę w przeglądarce: %s", url)
            if method == "own":
                report = run_capture_own_browser(
                    url, self._capture_login, self._capture_done, on_phase=self._set_phase,
                    extra_args=self.browser_args,
                )
            else:
                report = run_capture(url, timeout_minutes=30, done=self._capture_done)
            session = merge_and_save(report, cfg.session_file)
            self.capture_result = {
                "active": False, "requests": report.requests, "salt": bool(session.salt),
                "logged_in": session.logged_in, "login_template": bool(session.login_template),
                "notes": report.notes,
            }
            log.info("Połączenie zapisane: %d żądań, sól %s", report.requests,
                     "znaleziona" if session.salt else "NIE znaleziona")
        except BaseException as exc:  # run_capture raises SystemExit without Playwright
            self.capture_result = {"active": False, "error": str(exc) or type(exc).__name__}
            log.error("Łączenie z grą nie powiodło się: %s", exc)
        finally:
            self.mode = "stopped"

    def import_har(self, data: bytes) -> dict:
        from .capture import merge_and_save
        from .har import import_har

        with self._lock:
            if self.mode != "stopped":
                raise ValueError("Zatrzymaj bota przed łączeniem z grą.")
            self.mode, self.error = "capture", None
        try:
            cfg = self.config()
            report = import_har(data, fallback_page=f"https://{cfg.server}.herozerogame.com/")
            session = merge_and_save(report, cfg.session_file)
            self.capture_result = {
                "active": False, "requests": report.requests, "salt": bool(session.salt),
                "logged_in": session.logged_in, "login_template": bool(session.login_template),
                "notes": report.notes, "source": "har",
            }
            log.info("Zaimportowano połączenie z pliku HAR: %d żądań gry, sól %s", report.requests,
                     "znaleziona" if session.salt else "NIE znaleziona")
            return self.capture_result
        finally:
            self.mode = "stopped"

    def confirm_login(self) -> None:
        self._capture_login.set()

    def finish_capture(self) -> None:
        self._capture_login.set()  # also cancels a capture still waiting for login
        self._capture_done.set()

    def doctor(self) -> dict:
        cfg = self.config()
        if not Path(cfg.session_file).exists():
            return {"session": None, "actions": [], "extra": []}
        session = Session.load(cfg.session_file)
        rows, extra = check_actions(cfg, session)
        return {
            "session": {"url": session.request_url, "user_id": session.user_id, "logged_in": session.logged_in,
                        "salt": bool(session.salt), "base_params": sorted(session.base_params)},
            "actions": rows,
            "extra": extra,
        }

    def ping(self) -> dict:
        if self.mode != "stopped":
            raise ValueError("Test działa tylko przy zatrzymanym bocie.")
        cfg = self.config()
        session = Session.load(cfg.session_file)
        client = HeroZeroClient(session, retries=1)
        try:
            data = client.call(cfg.actions.sync, mutating=False)
        except GameError as exc:
            if not exc.is_session_error or not (cfg.email and cfg.password):
                raise
            client.login(cfg.email, cfg.password)
            session.save(cfg.session_file)
            data = client.call(cfg.actions.sync, mutating=False)
        c = data.get("character", {}) if isinstance(data.get("character"), dict) else {}
        return {"name": c.get("name"), "level": c.get("level"), "keys": sorted(data)}


def make_handler(ctrl: Controller, allowed_hosts: set[str] | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "hzbot"

        def log_message(self, fmt, *args):  # silence per-request logging
            pass

        # --- helpers ---

        def _host_ok(self) -> bool:
            if allowed_hosts is None:
                return True
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in allowed_hosts

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("oczekiwano obiektu JSON")
            return data

        # --- routes ---

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/status":
                    return self._json(ctrl.status())
                if url.path == "/api/logs":
                    after = int(parse_qs(url.query).get("after", ["0"])[0])
                    return self._json({"lines": ctrl.logs.since(after)})
                if url.path == "/api/config":
                    return self._json(ctrl.config_for_ui())
                if url.path == "/api/doctor":
                    return self._json(ctrl.doctor())
            except (ValueError, OSError) as exc:
                return self._json({"error": str(exc)}, 400)
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            # Custom header => browsers block cross-site requests (no CORS preflight answer).
            if not self._host_ok() or self.headers.get("X-HZBot") != "1":
                return self._send(403, b"forbidden", "text/plain")
            path = urlparse(self.path).path
            if path == "/api/import":
                return self._import()
            try:
                body = self._body()
                if path == "/api/start":
                    ctrl.start(str(body.get("kind", "game")), int(body.get("speed", 1)))
                elif path == "/api/stop":
                    ctrl.stop()
                elif path == "/api/config":
                    ctrl.save_config(body.get("config") or {})
                elif path == "/api/capture/start":
                    ctrl.start_capture(str(body.get("method", "own")))
                elif path == "/api/capture/logged_in":
                    ctrl.confirm_login()
                elif path == "/api/capture/finish":
                    ctrl.finish_capture()
                elif path == "/api/ping":
                    return self._json(ctrl.ping())
                else:
                    return self._send(404, b"not found", "text/plain")
            except (ValueError, OSError, GameError, TransportError, RuntimeError) as exc:
                return self._json({"error": str(exc)}, 400)
            self._json({"ok": True})

        def _import(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_UPLOAD:
                return self._json({"error": "Plik jest pusty albo za duży."}, 400)
            data = self.rfile.read(length)
            try:
                self._json(ctrl.import_har(data))
            except (ValueError, OSError) as exc:
                self._json({"error": str(exc)}, 400)
            finally:
                del data  # contains the user's password and session

    return Handler


def serve(config_path: str = "config.yaml", host: str = "127.0.0.1", port: int = 8777,
          open_browser: bool = True) -> None:
    ctrl = Controller(config_path)
    logging.getLogger("hzbot").setLevel(logging.INFO)
    logging.getLogger("hzbot").addHandler(ctrl.logs)
    try:
        cfg = ctrl.config()
        if cfg.log_file:
            fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            logging.getLogger("hzbot").addHandler(fh)
    except (ValueError, OSError) as exc:
        print(f"Uwaga: {exc}")

    local = host in ("127.0.0.1", "localhost", "::1")
    allowed = {"127.0.0.1", "localhost", "::1"} if local else None
    httpd = ThreadingHTTPServer((host, port), make_handler(ctrl, allowed))
    url = f"http://{'127.0.0.1' if local else host}:{port}/"
    print(f"Panel HZ Bot działa: {url}  (Ctrl+C aby zakończyć)")
    if not local:
        print("UWAGA: panel jest dostępny w sieci - każdy z dostępem może sterować Twoim kontem.")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.stop()
        httpd.server_close()
