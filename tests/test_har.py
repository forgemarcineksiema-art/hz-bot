import base64
import json
from urllib.parse import urlencode

import pytest

from hzbot import har
from hzbot.auth import make_auth

SALT = "hzSalt_42x"
URL = "https://pl1.herozerogame.com/request.php"


def post(form, as_params=False):
    if as_params:  # Firefox-style: only params, url-encoded values
        body = {"mimeType": "application/x-www-form-urlencoded",
                "params": [{"name": k, "value": urlencode({"x": v})[2:]} for k, v in form.items()]}
    else:
        body = {"mimeType": "application/x-www-form-urlencoded", "text": urlencode(form)}
    return {"request": {"method": "POST", "url": URL, "postData": body}, "response": {"content": {}}}


def form(action, uid="31", sid="sess31", **extra):
    f = {"action": action, "user_id": uid, "user_session_id": sid, "client_version": "html5_300",
         "auth": make_auth(action, uid, SALT)}
    f.update(extra)
    return f


def script(text, b64=False):
    content = {"mimeType": "application/javascript", "text": text}
    if b64:
        content = {"mimeType": "application/javascript", "encoding": "base64",
                   "text": base64.b64encode(text.encode()).decode()}
    return {"request": {"method": "GET", "url": "https://pl1.herozerogame.com/game.js"},
            "response": {"content": content}}


def make_har(*entries):
    return json.dumps({"log": {"version": "1.2", "entries": list(entries)}}).encode()


@pytest.mark.parametrize("as_params,b64", [(False, False), (True, True)])
def test_import_har(as_params, b64):
    data = make_har(
        script(f'var a="foo bar";var k="{SALT}";', b64=b64),
        post(form("loginUser", uid="0", sid="0", email="a@b.pl", password="p&ss=1"), as_params),
        post(form("syncGame"), as_params),
        post(form("startQuest", quest_id="5"), as_params),
    )
    rep = har.import_har(data)
    s = rep.session
    assert rep.salt_found and s.salt == SALT
    assert s.user_id == "31" and s.user_session_id == "sess31"
    assert s.base_params == {"client_version": "html5_300"}
    assert s.login_template["password"] == "{password}"
    assert "p&ss=1" not in json.dumps(s.__dict__)
    assert s.observed_actions["startQuest"] == ["quest_id"]


def test_captcha_login_is_not_templated():
    data = make_har(
        script(f'k="{SALT}"'),
        post(form("loginUser", uid="0", sid="0", email="a@b.pl", password="x", recaptcha_token="03AF...")),
        post(form("syncGame")),
    )
    rep = har.import_har(data)
    assert rep.session.login_template is None
    assert any("captcha" in n for n in rep.notes)
    assert rep.session.logged_in


def test_fetches_scripts_when_har_has_no_content(monkeypatch):
    calls = []

    def fake_fetch(page):
        calls.append(page)
        return [f'x="{SALT}"']

    monkeypatch.setattr(har, "fetch_sources", fake_fetch)
    rep = har.import_har(make_har(post(form("syncGame"))), fallback_page="https://pl1.herozerogame.com/")
    assert calls == ["https://pl1.herozerogame.com/"]
    assert rep.session.salt == SALT


def test_rejects_bad_files():
    with pytest.raises(ValueError, match="HAR"):
        har.import_har(b"not json")
    with pytest.raises(ValueError, match="żądań gry"):
        har.import_har(make_har(script("x")))
