"""Low-level client for ``request.php``."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Mapping

from .auth import make_auth
from .session import Session

log = logging.getLogger("hzbot.client")

# (url, form) -> decoded JSON response
Transport = Callable[[str, dict[str, str]], Any]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class GameError(Exception):
    """The server answered with a non-empty ``error`` field."""

    def __init__(self, code: str, action: str):
        super().__init__(f"{action}: {code}")
        self.code = code
        self.action = action

    @property
    def is_session_error(self) -> bool:
        c = self.code.lower()
        return any(k in c for k in ("session", "notauthorized", "not_authorized", "auth", "login"))


class TransportError(Exception):
    """Network failure or a response that is not valid JSON, after retries."""


def http_transport(timeout: float = 30.0) -> Transport:
    import requests

    http = requests.Session()
    http.headers.update({"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"})

    def send(url: str, form: dict[str, str]) -> Any:
        try:
            resp = http.post(url, data=form, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise TransportError(str(exc)) from exc

    return send


class HeroZeroClient:
    def __init__(
        self,
        session: Session,
        transport: Transport | None = None,
        *,
        on_data: Callable[[dict], None] | None = None,
        dry_run: bool = False,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session
        self.transport = transport or http_transport()
        self.on_data = on_data
        self.dry_run = dry_run
        self.retries = retries
        self._sleep = sleep

    def build_form(self, action: str, params: Mapping[str, Any] | None = None) -> dict[str, str]:
        s = self.session
        form = dict(s.base_params)
        form.update(
            action=action,
            user_id=str(s.user_id),
            user_session_id=s.user_session_id,
            auth=make_auth(action, s.user_id, s.salt),
        )
        for k, v in (params or {}).items():
            form[k] = str(v)
        return form

    def call(self, action: str, params: Mapping[str, Any] | None = None, *, mutating: bool = True) -> dict:
        if not action:
            raise ValueError("pusta nazwa akcji")
        if mutating and self.dry_run:
            log.info("[dry-run] pominięto %s %s", action, dict(params or {}))
            return {}
        return self._send(action, self.build_form(action, params))

    def _send(self, action: str, form: dict[str, str]) -> dict:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.transport(self.session.request_url, form)
                break
            except TransportError as exc:
                last = exc
                wait = min(60.0, 2.0 ** attempt) + random.random()
                log.warning("Błąd sieci przy %s (%s), ponowienie za %.0fs", action, exc, wait)
                self._sleep(wait)
        else:
            raise TransportError(f"{action}: {last}")

        if not isinstance(resp, dict):
            raise TransportError(f"{action}: nieoczekiwana odpowiedź {type(resp).__name__}")
        error = resp.get("error")
        if error:
            raise GameError(str(error), action)
        data = resp.get("data") or {}
        if not isinstance(data, dict):
            data = {"value": data}
        if self.on_data:
            self.on_data(data)
        return data

    def login(self, email: str, password: str) -> None:
        """Log in by replaying the captured login request with fresh credentials."""
        template = self.session.login_template
        if not template:
            raise RuntimeError("Brak szablonu logowania - uruchom najpierw `hzbot capture`.")
        if not email or not password:
            raise RuntimeError("Brak e-maila/hasła w konfiguracji (lub HZ_EMAIL/HZ_PASSWORD).")
        form = {k: v.replace("{email}", email).replace("{password}", password) for k, v in template.items()}
        action = form["action"]
        form["auth"] = make_auth(action, form.get("user_id", "0"), self.session.salt)
        data = self._send(action, form)

        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        user_id = user.get("id") or data.get("user_id")
        session_id = user.get("session_id") or user.get("user_session_id") or data.get("user_session_id")
        if not user_id or not session_id:
            raise GameError("login_response_without_session", action)
        self.session.user_id = str(user_id)
        self.session.user_session_id = str(session_id)
        log.info("Zalogowano jako użytkownik %s", user_id)
