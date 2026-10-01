from hzbot.auth import make_auth
from hzbot.capture import analyze

SALT = "abc123salt"


def form(action, uid="55", sid="sessX", **extra):
    f = {"action": action, "user_id": uid, "user_session_id": sid, "client_version": "html5_250",
         "auth": make_auth(action, uid, SALT)}
    f.update(extra)
    return f


def test_analyze_builds_session():
    forms = [
        form("loginUser", uid="0", sid="0", email="me@x.pl", password="hunter2", platform="web"),
        form("syncGame", rct="1"),
        form("startQuest", quest_id="12", rct="2"),
    ]
    rep = analyze("https://pl1.herozerogame.com/request.php", forms, [f'var s="{SALT}";'])
    s = rep.session
    assert rep.salt_found and rep.login_captured
    assert s.salt == SALT
    assert s.user_id == "55" and s.user_session_id == "sessX"
    assert s.base_params == {"client_version": "html5_250"}
    assert s.login_template["password"] == "{password}"
    assert s.login_template["email"] == "{email}"
    assert "hunter2" not in str(s.login_template)
    assert s.observed_actions["startQuest"] == ["quest_id", "rct"]
    assert any("rct" in n for n in rep.notes)


def test_analyze_without_salt_reports_note():
    rep = analyze("u", [form("syncGame")], ["nothing here"])
    assert not rep.salt_found
    assert rep.notes
