"""Load only explicitly named local probe credentials, never expose values."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

_LAB = {"AURA_LAB_API_TOKEN", "AURA_LAB_SESSION_COOKIE"}


def load_probe_credentials(project_root: Path) -> None:
    for path in (project_root / ".local" / "ops" / "credentials.env",
                 project_root / ".local" / "ops-lab" / "credentials.env",
                 project_root / ".local" / "ops-healthchecks" / "credentials.env",
                 project_root / ".env"):
        if not path.is_file() or path.stat().st_size > 65536:
            continue
        for key, value in dotenv_values(path, interpolate=False).items():
            if (key in _LAB or key.startswith("AURA_TARGET_")) and isinstance(value, str) and value:
                os.environ.setdefault(key, value)
