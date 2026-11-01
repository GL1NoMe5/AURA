"""Bounded data redaction before operational evidence leaves its collector.

This is defense in depth. Collectors must still select fields explicitly and
must never include complete container environments, credentials, or bodies.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"
_KEY = re.compile(
    r"(?:^|[_\-.])(?:password|passwd|pwd|secret|token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|credential|credentials|authorization|"
    r"cookie|cookies|session[_-]?id|passphrase)(?:$|[_\-.])", re.I)
_ASSIGNMENT = re.compile(
    r'''(?P<prefix>["']?(?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization|cookie|passphrase)[\w.-]*|pwd)["']?\s*[:=]\s*)(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&}\]\r\n]+)''',
    re.I)
_AUTH = re.compile(r"\b(?:Bearer|Token|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_URL_AUTH = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.I)
_PEM = re.compile(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----.*?"
                  r"-----END (?:[A-Z ]*PRIVATE KEY)-----", re.S)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\b")


def is_sensitive_key(key: str) -> bool:
    """Recognize common credential field/environment names."""
    normalized = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(key))
    return bool(_KEY.search(normalized))


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    value = str(text)
    value = _PEM.sub(REDACTED, value)
    value = _URL_AUTH.sub(r"\1[REDACTED]@", value)
    value = _AUTH.sub(REDACTED, value)
    value = _JWT.sub(REDACTED, value)
    value = _ASSIGNMENT.sub(lambda m: m.group("prefix") + REDACTED, value)
    for secret in sorted({str(s) for s in secrets if s}, key=len, reverse=True):
        value = value.replace(secret, REDACTED)
    return value


def redact_data(data: Any, secrets: Iterable[str] = ()) -> Any:
    """Return a redacted JSON-compatible copy without changing its schema."""
    known = tuple(secrets)
    if isinstance(data, Mapping):
        return {str(k): REDACTED if is_sensitive_key(str(k))
                else redact_data(v, known) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact_data(value, known) for value in data]
    if isinstance(data, str):
        return redact_text(data, known)
    return data
