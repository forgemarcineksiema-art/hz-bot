"""Learn the protocol from the official web client.

Opens the game in a real browser, lets the user log in and play for a moment,
records every ``request.php`` call and the loaded JavaScript, then derives:
the endpoint, user id / session id, constant parameters (client_version...),
a login template and the auth salt.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass, field
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

        sources = []
        for frame in (page.frames if not page.is_closed() else []):
            try:
                sources.append(frame.content())
            except Exception:  # detached frame
                pass
        for s_url in dict.fromkeys(script_urls):
            try:
                r = context.request.get(s_url)
                if r.ok:
                    sources.append(r.text())
            except Exception as exc:
                log.debug("Nie pobrano %s: %s", s_url, exc)
        try:
            browser.close()
        except Exception:
            pass

    request_url = request_urls[-1] if request_urls else url.rstrip("/") + "/request.php"
    return analyze(request_url, forms, sources)


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
