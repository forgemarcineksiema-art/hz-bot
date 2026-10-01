"""Import the game protocol from a HAR file saved in the user's own browser.

This is the alternative to :func:`hzbot.capture.run_capture` for when the game
shows a captcha: the user logs in normally in their everyday browser (solving
the captcha themselves), saves the network log as HAR from the developer tools
and the bot learns everything it needs from that file.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from urllib.parse import parse_qsl, unquote_plus, urljoin

from .capture import CaptureReport, analyze

log = logging.getLogger("hzbot.har")

_SCRIPT_SRC = re.compile(r"""<script[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _form(post: dict) -> dict[str, str]:
    text = post.get("text")
    if text:
        if text.lstrip().startswith("{"):  # JSON body, just in case
            try:
                data = json.loads(text)
                return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            except ValueError:
                return {}
        return dict(parse_qsl(text, keep_blank_values=True))
    return {unquote_plus(p.get("name", "")): unquote_plus(p.get("value", "")) for p in post.get("params", [])}


def _content_text(content: dict) -> str:
    text = content.get("text") or ""
    if text and content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", "replace")
        except ValueError:
            return ""
    return text


def parse_har(data: bytes | str) -> tuple[str, list[dict[str, str]], list[str], str]:
    """Return (request_url, forms, sources, page_url) from a HAR document."""
    try:
        har = json.loads(data)
        entries = har["log"]["entries"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("To nie jest poprawny plik HAR (zapisz go z narzędzi deweloperskich przeglądarki).")

    request_url = ""
    page_url = ""
    forms: list[dict[str, str]] = []
    sources: list[str] = []
    for e in entries:
        req, resp = e.get("request", {}), e.get("response", {})
        url = req.get("url", "").split("?")[0]
        if req.get("method") == "POST" and url.endswith("request.php"):
            form = _form(req.get("postData") or {})
            if form.get("action"):
                forms.append(form)
                request_url = url
            continue
        content = resp.get("content") or {}
        mime = (content.get("mimeType") or "").lower()
        if "javascript" in mime or url.endswith(".js") or "html" in mime:
            text = _content_text(content)
            if text:
                sources.append(text)
            if "html" in mime and "herozerogame" in url and not page_url:
                page_url = url
    if not forms:
        raise ValueError(
            "W pliku nie ma żadnych żądań gry. Otwórz narzędzia deweloperskie (F12) PRZED zalogowaniem, "
            "włącz „Zachowaj log” i zagraj chwilę, zanim zapiszesz HAR."
        )
    return request_url, forms, sources, page_url


def fetch_sources(page_url: str, limit: int = 25) -> list[str]:
    """Download the game page and its scripts (used when the HAR has no file contents)."""
    import requests

    from .client import USER_AGENT

    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT
    page = http.get(page_url, timeout=30)
    page.raise_for_status()
    sources = [page.text]
    for src in list(dict.fromkeys(_SCRIPT_SRC.findall(page.text)))[:limit]:
        try:
            r = http.get(urljoin(page_url, src), timeout=30)
            if r.ok:
                sources.append(r.text)
        except requests.RequestException as exc:
            log.debug("Nie pobrano %s: %s", src, exc)
    return sources


def import_har(data: bytes | str, fallback_page: str = "") -> CaptureReport:
    request_url, forms, sources, page_url = parse_har(data)
    report = analyze(request_url, forms, sources)
    page = page_url or fallback_page
    if not report.salt_found and page:
        log.info("Brak kodu gry w pliku HAR - pobieram skrypty z %s", page)
        try:
            report = analyze(request_url, forms, sources + fetch_sources(page))
        except Exception as exc:  # network problems must not hide the partial result
            log.warning("Nie udało się pobrać skryptów gry: %s", exc)
    return report
