"""Strict typed JSON-lines protocol validation for the worker boundary.

Requests:  {"id": <str>, "command": <str>, "params": {} (optional)}
Responses: {"id": <str|null>, "ok": true, "result": {...}}
           {"id": <str|null>, "ok": false, "error": {"code", "message"}}
Input is never executed as shell/code/paths; unknown keys are rejected.
"""
from __future__ import annotations

import json
from typing import Any, Optional

COMMANDS = frozenset(
    {"initialize", "inject_cache_retention", "restore_cache_policy",
     "status", "shutdown"}
)
MAX_LINE_BYTES = 8192
MAX_ID_LENGTH = 128
MAX_ERROR_MESSAGE = 256
_ALLOWED_TOP_KEYS = frozenset({"id", "command", "params"})


class ProtocolError(Exception):
    def __init__(self, code: str, message: str, request_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message[:MAX_ERROR_MESSAGE]
        self.request_id = request_id


def parse_request_line(line: str) -> tuple[str, str, dict]:
    """Validate one raw line; returns (request_id, command, params)."""
    if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
        raise ProtocolError("line_too_large", "request line exceeds size limit")
    stripped = line.strip()
    if not stripped:
        raise ProtocolError("malformed_request", "empty line")
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        raise ProtocolError("malformed_request", "line is not valid JSON") from None
    if not isinstance(obj, dict):
        raise ProtocolError("malformed_request", "request must be a JSON object")
    unknown_keys = set(obj) - _ALLOWED_TOP_KEYS
    if unknown_keys:
        raise ProtocolError(
            "malformed_request",
            f"unexpected top-level keys: {sorted(unknown_keys)[:4]}",
        )
    request_id = obj.get("id")
    if not isinstance(request_id, str) or not (1 <= len(request_id) <= MAX_ID_LENGTH):
        raise ProtocolError(
            "malformed_request", "id must be a non-empty string (<=128 chars)"
        )
    command = obj.get("command")
    if not isinstance(command, str) or not command:
        raise ProtocolError(
            "malformed_request", "command must be a non-empty string", request_id
        )
    if command not in COMMANDS:
        raise ProtocolError("unknown_command", f"unknown command: {command[:64]}",
                            request_id)
    params = obj.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError("invalid_params", "params must be an object", request_id)
    if params:
        # Bounded commands take no parameters in v1; reject anything else so
        # paths/code/shell payloads can never be smuggled into the worker.
        raise ProtocolError(
            "invalid_params", "this command takes no params", request_id
        )
    return request_id, command, params


def make_success(request_id: Optional[str], result: dict) -> str:
    return json.dumps({"id": request_id, "ok": True, "result": result},
                      separators=(",", ":"))


def make_error(request_id: Optional[str], code: str, message: str) -> str:
    return json.dumps(
        {"id": request_id, "ok": False,
         "error": {"code": code, "message": message[:MAX_ERROR_MESSAGE]}},
        separators=(",", ":"),
    )


def parse_response_line(line: str) -> dict[str, Any]:
    """Parent-side response validation; raises ValueError on bad framing."""
    obj = json.loads(line)
    if not isinstance(obj, dict) or "ok" not in obj or "id" not in obj:
        raise ValueError("response must be an object with id and ok")
    if obj["ok"] is True and not isinstance(obj.get("result"), dict):
        raise ValueError("success response must carry an object result")
    if obj["ok"] is False:
        err = obj.get("error")
        if not isinstance(err, dict) or "code" not in err or "message" not in err:
            raise ValueError("error response must carry code and message")
    if obj["ok"] not in (True, False):
        raise ValueError("ok must be boolean")
    return obj
