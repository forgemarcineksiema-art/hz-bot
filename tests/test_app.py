import json
import logging
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from hzbot import app as app_mod
from hzbot.auth import make_auth
from hzbot.capture import analyze
from hzbot.config import Config
from hzbot.session import Session


@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HZ_PASSWORD", raising=False)
    monkeypatch.delenv("HZ_EMAIL", raising=False)
    c = app_mod.Controller(str(tmp_path / "config.yaml"))
    logger = logging.getLogger("hzbot")
    logger.addHandler(c.logs)  # as serve() does
    monkeypatch.setattr(logger, "level", logging.INFO)
    yield c
    c.stop()
    logger.removeHandler(c.logs)


@pytest.fixture
def server(ctrl):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_mod.make_handler(ctrl, {"127.0.0.1", "localhost"}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None, headers=None):
    h = {"Content-Type": "application/json", "X-HZBot": "1"} if body is not None else {}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=h, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_save_config_keeps_password_unless_changed(ctrl, tmp_path):
    cfg = ctrl.config_for_ui()["config"]
    cfg["password"] = "first"
    ctrl.save_config(cfg)
    cfg = ctrl.config_for_ui()["config"]
    assert cfg["password"] == ""  # never sent back to the browser
    cfg["server"] = "de3"
    ctrl.save_config(cfg)
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert raw["password"] == "first" and raw["server"] == "de3"

    cfg["quests"]["strategy"] = 5
    with pytest.raises(ValueError):
        ctrl.save_config(cfg)
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["server"] == "de3"


def test_http_security(server):
    assert call(server, "/api/status")["mode"] == "stopped"
    with pytest.raises(urllib.error.HTTPError) as e:  # no custom header = possible cross-site request
        urllib.request.urlopen(urllib.request.Request(server + "/api/stop", data=b"{}", method="POST"))
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:  # DNS rebinding
        call(server, "/api/status", headers={"Host": "evil.example"})
    assert e.value.code == 403


def test_real_game_requires_setup(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server, "/api/start", {"kind": "game"})
    assert e.value.code == 400


def test_simulation_start_and_stop(server, ctrl):
    call(server, "/api/start", {"kind": "sim", "speed": 1200})
    deadline = time.time() + 10
    while time.time() < deadline:
        st = call(server, "/api/status")
        if (st["bot"] or {}).get("stats", {}).get("quests_completed"):
            break
        time.sleep(0.2)
    assert st["mode"] == "running" and st["run_kind"] == "sim"
    assert st["bot"]["character"]["name"] == "SimHero"
    assert st["bot"]["gained"]["xp"] > 0
    call(server, "/api/stop", {})
    st = call(server, "/api/status")
    assert st["mode"] == "stopped" and st["error"] is None
    assert any("Misja" in line["msg"] for line in call(server, "/api/logs?after=0")["lines"])


def test_capture_from_panel(ctrl, monkeypatch, tmp_path):
    salt = "s3cr3tSalt"
    forms = [{"action": "syncGame", "user_id": "9", "user_session_id": "abc", "client_version": "v1",
              "auth": make_auth("syncGame", "9", salt)}]

    def fake_capture(url, timeout_minutes, done):
        assert url == "https://pl1.herozerogame.com/"
        assert done.wait(5)
        return analyze(url + "request.php", forms, [f'x="{salt}"'])

    monkeypatch.setattr("hzbot.capture.run_capture", fake_capture)
    ctrl.start_capture()
    assert ctrl.status()["mode"] == "capture"
    ctrl.finish_capture()
    for _ in range(50):
        if ctrl.mode == "stopped":
            break
        time.sleep(0.05)
    assert ctrl.capture_result["salt"] and ctrl.capture_result["logged_in"]
    assert Session.load(tmp_path / Config().session_file).salt == salt
    assert ctrl.setup_state()["ready"]
    assert ctrl.doctor()["actions"][1]["status"] == "ok"  # sync observed


def test_import_har_over_http(server, ctrl, tmp_path, monkeypatch):
    from tests.test_har import SALT, form, make_har, post, script

    data = make_har(script(f'k="{SALT}"'), post(form("syncGame")))
    req = urllib.request.Request(server + "/api/import", data=data, method="POST",
                                 headers={"X-HZBot": "1", "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=5) as r:
        result = json.loads(r.read())
    assert result["salt"] and result["logged_in"]
    assert ctrl.setup_state()["ready"]
    assert ctrl.status()["mode"] == "stopped"

    req = urllib.request.Request(server + "/api/import", data=b"garbage", method="POST", headers={"X-HZBot": "1"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
