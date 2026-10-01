import hashlib

from hzbot.auth import extract_string_literals, find_salt, make_auth


def test_make_auth_is_md5_of_action_salt_user():
    assert make_auth("syncGame", 42, "s4lt") == hashlib.md5(b"syncGames4lt42").hexdigest()


def test_extract_literals_handles_quote_styles():
    js = """var a="abcd",b='efgh';c=`ijkl`;d="x";e="esc\\"aped";"""
    lits = extract_string_literals(js)
    assert {"abcd", "efgh", "ijkl"} <= lits
    assert "x" not in lits


def test_find_salt_from_js_literals():
    salt = "Zq9xK2pL"
    samples = [
        {"action": "syncGame", "user_id": "7", "auth": make_auth("syncGame", "7", salt)},
        {"action": "startQuest", "user_id": "7", "auth": make_auth("startQuest", "7", salt)},
    ]
    js = f'var x="hello world";var k="{salt}";var y="other";'
    assert find_salt(samples, sorted(extract_string_literals(js))) == salt
    assert find_salt(samples, ["nope", "wrong"]) is None
    assert find_salt([], [salt]) is None
