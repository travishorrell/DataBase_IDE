"""Small JSON-backed app settings (~/.dbide/settings.json)."""
from __future__ import annotations

import json
from pathlib import Path

from dbide.core.config import app_dir

DEFAULTS = {
    "beginner_mode": True,
    "secret_backend": "keychain",  # keychain | vault | session
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "settings.json"
        self.data = dict(DEFAULTS)
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, json.JSONDecodeError):
            pass

    def get(self, key: str):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        self.data[key] = value
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
