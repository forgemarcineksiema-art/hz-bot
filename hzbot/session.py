"""Persistent connection data: endpoint, credentials of the current session, salt."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Session:
    request_url: str
    user_id: str = "0"
    user_session_id: str = ""
    salt: str = ""
    # Parameters the official client sends with every request (client_version, ...).
    base_params: dict[str, str] = field(default_factory=dict)
    # Captured login request with "{email}" / "{password}" placeholders.
    login_template: dict[str, str] | None = None
    # action name -> parameter names seen in captured traffic.
    observed_actions: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def for_server(cls, server: str) -> "Session":
        return cls(request_url=f"https://{server}.herozerogame.com/request.php")

    @classmethod
    def load(cls, path: str | Path) -> "Session":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # contains a live session id
        except OSError:
            pass

    @property
    def logged_in(self) -> bool:
        return bool(self.user_session_id) and self.user_id not in ("", "0")
