"""Compare configured action names with what the official client was seen sending."""

from __future__ import annotations

from dataclasses import asdict

from .config import Config
from .session import Session

OK, UNSEEN, DISABLED = "ok", "unseen", "disabled"


def check_actions(cfg: Config, session: Session) -> tuple[list[dict], list[dict]]:
    """Return (configured actions with status, other actions seen in game traffic)."""
    observed = session.observed_actions
    configured = asdict(cfg.actions)
    rows = []
    for name, action in configured.items():
        status = DISABLED if not action else OK if action in observed else UNSEEN
        rows.append({"name": name, "action": action, "status": status, "params": observed.get(action, [])})
    extra = [
        {"action": a, "params": observed[a]}
        for a in sorted(set(observed) - set(configured.values()))
    ]
    return rows, extra
