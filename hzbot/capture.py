"""Learn the protocol from the official web client.

Opens the game in a real browser, lets the user log in and play for a moment,
records every ``request.php`` call and the loaded JavaScript, then derives:
the endpoint, user id / session id, constant parameters (client_version...),
a login template and the auth salt.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl

from .auth import extract_string_literals, find_salt
from .session import Session

log = logging.getLogger("hzbot.capture")

STANDARD = {"action", "auth", "user_id", "user_session_id"}


@dataclass
class CaptureReport:
    session: Session
    requests: int
    salt_found: bool
    login_captured: bool
    notes: list[str] = field(default_factory=list)


def _is_login(form: dict[str, str]) -> bool:
    return "login" in form.get("action", "").lower() and any("pass" in k.lower() for k in form)


def _login_template(form: dict[str, str]) -> dict[str, str]:
    tpl = {}
    for k, v in form.items():
        kl = k.lower()
        if "pass" in kl:
            tpl[k] = "{password}"
        elif "@" in v or kl in ("email", "login", "username"):
            tpl[k] = "{email}"
        elif k != "auth":
            tpl[k] = v
    return tpl


def analyze(request_url: str, forms: list[dict[str, str]], sources: list[str]) -> CaptureReport:
    forms = [f for f in forms if f.get("action")]
    notes: list[str] = []
    session = Session(request_url=request_url)

    login_forms = [f for f in forms if _is_login(f)]
    if login_forms:
        if any("captcha" in k.lower() for k in login_forms[-1]):
            # A captcha token is single-use: replaying this login would never work.
            notes.append(
                "Logowanie w grze wymaga captcha, więc bot nie będzie logował się sam. "
                "Gdy sesja wygaśnie, połącz bota z grą ponownie."
            )
            login_forms = []
        else:
            session.login_template = _login_template(login_forms[-1])
    game_forms = [f for f in forms if not _is_login(f)]
    authed = [f for f in game_forms if f.get("user_session_id") not in (None, "", "0")]
    if authed:
        session.user_id = authed[-1].get("user_id", "0")
        session.user_session_id = authed[-1]["user_session_id"]
    else:
        notes.append("Nie przechwycono żadnego zalogowanego żądania - zaloguj się w oknie przeglądarki.")

    if game_forms:
        common = set(game_forms[0]) - STANDARD
        for f in game_forms[1:]:
            common &= set(f)
        constant = {k for k in common if len({f[k] for f in game_forms}) == 1}
        session.base_params = {k: game_forms[-1][k] for k in sorted(constant)}
        volatile = sorted(common - constant)
        if volatile:
            notes.append(
                "Parametry zmienne między żądaniami (nieobsługiwane automatycznie): " + ", ".join(volatile)
            )

    for f in forms:
        names = sorted(set(f) - STANDARD - set(session.base_params))
        if _is_login(f):
            names = sorted(set(f) - {"auth", "action"})
        prev = set(session.observed_actions.get(f["action"], []))
        session.observed_actions[f["action"]] = sorted(prev | set(names))

    candidates: set[str] = set()
    for src in sources:
        candidates |= extract_string_literals(src)
    salt = find_salt(forms, sorted(candidates))
    if salt is not None:
        session.salt = salt
    else:
        notes.append(
            "Nie udało się ustalić soli podpisu (auth). Podaj ją ręcznie: `hzbot capture --salt ...` "
            "lub wpisz \"salt\" w pliku sesji."
        )
    return CaptureReport(session, len(forms), salt is not None, bool(login_forms), notes)


def run_capture(
    url: str,
    headless: bool = False,
    timeout_minutes: float = 20,
    done: threading.Event | None = None,
) -> CaptureReport:
    """Capture until Enter is pressed - or until ``done`` is set, when given (GUI)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "Brak Playwright. Zainstaluj: pip install playwright && playwright install chromium"
        ) from None

    forms: list[dict[str, str]] = []
    request_urls: list[str] = []
    script_urls: list[str] = []

    def on_request(req) -> None:
        if req.method == "POST" and "request.php" in req.url:
            forms.append(dict(parse_qsl(req.post_data or "", keep_blank_values=True)))
            request_urls.append(req.url.split("?")[0])

    def on_response(resp) -> None:
        ctype = resp.headers.get("content-type", "")
        if "javascript" in ctype or resp.url.split("?")[0].endswith(".js"):
            script_urls.append(resp.url)

    interactive = done is None
    done = done or threading.Event()

    def wait_enter() -> None:
        # On EOF (no terminal) keep capturing until the timeout instead.
        if sys.stdin.readline():
            done.set()

    with sync_playwright() as pw:
        # HZ_BROWSER: path to a Chromium binary, if Playwright's own is not installed.
        browser = pw.chromium.launch(headless=headless, executable_path=os.environ.get("HZ_BROWSER") or None)
        context = browser.new_context()
        context.on("request", on_request)
        context.on("response", on_response)
        page = context.new_page()
        page.goto(url)
        if interactive:
            print(
                "\nW otwartym oknie przeglądarki:\n"
                "  1. Zaloguj się na swoje konto (e-mail + hasło).\n"
                "  2. Wejdź do gry i wykonaj kilka akcji: rozpocznij misję, otwórz pojedynki itp.\n"
                "  3. Wróć tutaj i naciśnij Enter.\n"
            )
            threading.Thread(target=wait_enter, daemon=True).start()
        waited = 0.0
        while not done.is_set() and waited < timeout_minutes * 60:
            try:
                page.wait_for_timeout(500)  # keeps Playwright's event loop running
            except Exception:  # user closed the browser window
                break
            waited += 0.5

        sources = _collect_sources(context, script_urls)
        try:
            browser.close()
        except Exception:
            pass

    request_url = request_urls[-1] if request_urls else url.rstrip("/") + "/request.php"
    return analyze(request_url, forms, sources)


def _collect_sources(context, script_urls: list[str]) -> list[str]:
    """HTML of every open frame plus the game's JavaScript files."""
    sources: list[str] = []
    urls = list(script_urls)
    for page in context.pages:
        if page.is_closed():
            continue
        for frame in page.frames:
            try:
                sources.append(frame.content())
                urls += frame.evaluate(
                    "performance.getEntriesByType('resource').map(e => e.name)"
                    ".filter(u => u.split('?')[0].endsWith('.js'))"
                )
            except Exception:  # detached or cross-process frame
                pass
    for s_url in dict.fromkeys(urls):
        try:
            r = context.request.get(s_url)
            if r.ok:
                sources.append(r.text())
        except Exception as exc:
            log.debug("Nie pobrano %s: %s", s_url, exc)
    return sources


# --- capture in the user's own browser ---------------------------------------
#
# The browser above is launched *by* Playwright and flagged as automated, so the
# game's "I'm not a robot" captcha may refuse it. Here we instead start the
# user's installed Chrome/Edge as a normal process (own profile, no automation
# flags). Nothing is attached while the user logs in; only afterwards do we
# connect to its debugging port - like DevTools does - and record the traffic.

PROFILE_DIR = Path.home() / ".hzbot" / "browser-profile"


def find_browser() -> str | None:
    """Path to an installed Chrome / Edge / Chromium (``HZ_BROWSER`` overrides)."""
    env = os.environ.get("HZ_BROWSER")
    if env:
        return env
    candidates: list[Path] = []
    if sys.platform == "win32":
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates += [
                    Path(base, "Google", "Chrome", "Application", "chrome.exe"),
                    Path(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                ]
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"):
            candidates.append(Path("/Applications", f"{app}.app", "Contents", "MacOS", app))
    for c in candidates:
        if c.exists():
            return str(c)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "microsoft-edge", "microsoft-edge-stable", "brave-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_debugger(port: int, proc: subprocess.Popen, timeout: float = 30) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Przeglądarka zamknęła się zaraz po uruchomieniu.")
        try:
            opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("Przeglądarka nie odpowiada - spróbuj ponownie albo użyj importu pliku HAR.")


def _wait_event(event: threading.Event, proc: subprocess.Popen, deadline: float) -> None:
    while not event.wait(0.5):
        if proc.poll() is not None:
            raise RuntimeError("Okno przeglądarki zostało zamknięte przed zakończeniem.")
        if time.time() > deadline:
            raise RuntimeError("Przekroczono czas oczekiwania.")


def run_capture_own_browser(
    url: str,
    logged_in: threading.Event,
    done: threading.Event,
    timeout_minutes: float = 30,
    on_phase: Callable[[str], None] | None = None,
    extra_args: tuple[str, ...] = (),
) -> CaptureReport:
    """Two phases: the user logs in (nothing attached), then plays while we record.

    ``logged_in`` / ``done`` are set by the caller (panel buttons or Enter in CLI).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Brak Playwright. Zainstaluj: pip install playwright") from None

    exe = find_browser()
    if not exe:
        raise RuntimeError(
            "Nie znaleziono Chrome ani Edge. Zainstaluj jedną z nich, ustaw HZ_BROWSER "
            "na ścieżkę przeglądarki albo użyj importu pliku HAR."
        )
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    deadline = time.time() + timeout_minutes * 60
    phase = on_phase or (lambda _p: None)
    log.info("Uruchamiam przeglądarkę: %s", exe)
    proc = subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", *extra_args, url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    forms: list[dict[str, str]] = []
    request_urls: list[str] = []
    script_urls: list[str] = []

    def on_request(req) -> None:
        if req.method == "POST" and "request.php" in req.url:
            forms.append(dict(parse_qsl(req.post_data or "", keep_blank_values=True)))
            request_urls.append(req.url.split("?")[0])

    def on_response(resp) -> None:
        if resp.url.split("?")[0].endswith(".js"):
            script_urls.append(resp.url)

    try:
        _wait_for_debugger(port, proc)
        phase("login")
        _wait_event(logged_in, proc, deadline)  # user logs in; nothing is connected yet

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            context.on("request", on_request)
            context.on("response", on_response)
            phase("play")
            while not done.is_set():
                if time.time() > deadline:
                    raise RuntimeError("Przekroczono czas oczekiwania.")
                pages = [p for p in context.pages if not p.is_closed()]
                if not pages:
                    break  # window closed - analyse what we have
                try:
                    pages[0].wait_for_timeout(500)  # keeps Playwright's event loop running
                except Exception:
                    break
            sources = _collect_sources(context, script_urls)
            try:
                browser.close()  # only disconnects from the user's browser
            except Exception:
                pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    request_url = request_urls[-1] if request_urls else url.rstrip("/") + "/request.php"
    report = analyze(request_url, forms, sources)
    if not forms:
        report.notes.append(
            "Nie zarejestrowano żadnych akcji gry - po kliknięciu „Zalogowałem się” rozpocznij misję "
            "albo pojedynek, zanim klikniesz „Gotowe”."
        )
    return report


def merge_and_save(report: CaptureReport, path: str, salt: str = "") -> Session:
    """Save the captured session, keeping actions observed in earlier captures."""
    from pathlib import Path

    session = report.session
    if salt:
        session.salt = salt
    previous = Path(path)
    if previous.exists():
        old = Session.load(previous)
        for action, names in old.observed_actions.items():
            session.observed_actions[action] = sorted(set(names) | set(session.observed_actions.get(action, [])))
        if not session.salt:
            session.salt = old.salt
        if not session.login_template:
            session.login_template = old.login_template
    session.save(path)
    return session
