"""Request signing used by the Hero Zero game server.

Every call to ``request.php`` carries ``auth = md5(action + salt + user_id)``.
The salt is embedded in the official web client, so instead of hard-coding it
we recover it from captured traffic (see :mod:`hzbot.capture`).
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Mapping

# Quoted string literals in JavaScript/HTML sources, 4..64 chars, single line.
_LITERAL_RE = re.compile(r"""(["'`])((?:(?!\1)[^\\\r\n]){4,64})\1""")


def make_auth(action: str, user_id: str | int, salt: str) -> str:
    return hashlib.md5(f"{action}{salt}{user_id}".encode("utf-8")).hexdigest()


def extract_string_literals(source: str) -> set[str]:
    return {m.group(2) for m in _LITERAL_RE.finditer(source)}


def find_salt(samples: Iterable[Mapping[str, str]], candidates: Iterable[str]) -> str | None:
    """Return the candidate salt that reproduces ``auth`` of every sample.

    ``samples`` are captured request forms (need ``action``, ``user_id``, ``auth``).
    """
    signed = [s for s in samples if s.get("auth") and s.get("action")]
    if not signed:
        return None
    first, rest = signed[0], signed[1:]
    target = first["auth"].lower()
    for cand in candidates:
        if make_auth(first["action"], first.get("user_id", ""), cand) != target:
            continue
        if all(
            make_auth(s["action"], s.get("user_id", ""), cand) == s["auth"].lower()
            for s in rest
        ):
            return cand
    return None
