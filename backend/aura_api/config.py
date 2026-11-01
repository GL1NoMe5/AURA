"""Local-only API configuration: origins and provider-key presence."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PROVIDER_ENV_VAR = "BASETEN_API_KEY"


def is_origin_allowed(origin: str | None) -> bool:
    """CLI/no-Origin requests are allowed; browser origins must be local."""
    if origin is None or origin == "":
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https", "ws", "wss"):
        return False
    return parsed.hostname in _LOCAL_HOSTS


def detect_provider_configured(project_root: Path) -> bool:
    """Presence of a provider key only; the value is never read out/logged."""
    if os.environ.get(PROVIDER_ENV_VAR, "").strip():
        return True
    env_file = project_root / ".env"
    try:
        if env_file.is_file():
            for line in env_file.read_text(
                    encoding="utf-8", errors="ignore").splitlines():
                name, sep, value = line.partition("=")
                if sep and name.strip() == PROVIDER_ENV_VAR \
                        and value.strip().strip("'\""):
                    return True
    except OSError:
        pass
    return False


@dataclass
class ApiSettings:
    detection_timeout_seconds: float = 30.0
    failure_evidence_timeout_seconds: float = 60.0
    orchestration_timeout_seconds: float = 120.0
    verification_timeout_seconds: float = 45.0
    telemetry_interval_seconds: float = 0.5
    subscriber_queue_size: int = 100
    journal_maxlen: int = 500
