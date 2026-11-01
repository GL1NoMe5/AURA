"""Baseten capability preflight for AURA (SETUP-002).

Default mode is offline configuration checking and makes zero provider
requests. Live checks run only with --live and make at most three requests
(text, streaming, vision) against the exact runtime model alias.

Docs checked 2026-10-27 (docs.baseten.co):
- Chat Completions is OpenAI-compatible at https://inference.baseten.co/v1
  (POST /v1/chat/completions); output ceiling uses `max_tokens`
  (default 4096 in the reference schema).
- Streaming uses `stream: true`; token usage in streams requires
  `stream_options.include_usage: true` and arrives on the final chunk.
  Stream choices carry `delta.content`.
- Reasoning models may return `reasoning_content` separately from
  `content`; Muse Spark Open always reasons and takes no effort control.
- Vision uses the OpenAI `image_url` content type (`detail: auto`);
  local files are sent as base64 data URIs inside the request body.
- Usage is `UsageInfo` (prompt_tokens / completion_tokens / total_tokens,
  with completion_tokens_details.reasoning_tokens when reported).
Missing usage or reasoning is recorded as unavailable, never as zero.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - offline mode must still work
    OpenAI = None  # type: ignore[assignment]

MODEL = "meta-models/muse-spark-open-1026"
BASE_URL = "https://inference.baseten.co/v1"
MAX_TOKENS_CEILING = 4096
REQUEST_MAX_TOKENS = 1024
TIMEOUT_SECONDS = 30.0
MAX_LIVE_REQUESTS = 3
FIXTURE_RELATIVE = Path("tools") / "preflight" / "fixtures" / "vision-probe.png"
DEFAULT_OUTPUT_RELATIVE = Path(".local") / "preflight" / "baseten-capabilities.json"
BLOCKED_MISSING_KEY = "ONLINE_CHECKS_BLOCKED_MISSING_KEY"
NEXT_LIVE_COMMAND = (
    r"backend\.venv\Scripts\python.exe tools\preflight\baseten_probe.py --live"
)
TEXT_PROMPT = "Reply with exactly: AURA preflight OK"
STREAM_PROMPT = "Count from 1 to 5 separated by spaces. Reply with only the numbers."
VISION_PROMPT = (
    "This image has a top half and a bottom half, each a solid color. "
    "Name the top-half color first, then the bottom-half color. "
    "Answer concisely using the labels 'top:' and 'bottom:'."
)
_UNSET = object()


def get_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load_api_key(project_root: Optional[Path] = None) -> Optional[str]:
    """Return the configured key or None. Never logs or prints the value."""
    env_value = os.environ.get("BASETEN_API_KEY")
    if _is_nonempty(env_value):
        return env_value.strip()  # type: ignore[union-attr]
    root = project_root or get_project_root()
    env_path = root / ".env"
    if env_path.is_file():
        try:
            from dotenv import dotenv_values

            file_value = dotenv_values(env_path).get("BASETEN_API_KEY")
        except Exception:
            return None
        if _is_nonempty(file_value):
            return file_value.strip()  # type: ignore[union-attr]
    return None


def get_config(project_root: Optional[Path] = None) -> dict[str, Any]:
    root = project_root or get_project_root()
    fixture = root / FIXTURE_RELATIVE
    present = fixture.is_file()
    return {
        "model": MODEL,
        "base_url": BASE_URL,
        "api_key_configured": load_api_key(root) is not None,
        "fixture_path": FIXTURE_RELATIVE.as_posix(),
        "fixture_present": present,
        "fixture_size_bytes": fixture.stat().st_size if present else None,
        "request_max_tokens": REQUEST_MAX_TOKENS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_live_requests": MAX_LIVE_REQUESTS,
        "sdk_max_retries": 0,
    }


_ERROR_DETAILS = {
    "auth_error": "authentication failed; check BASETEN_API_KEY configuration",
    "rate_limit": "rate limit reached (429)",
    "timeout": "request timed out",
    "connection_error": "connection to provider failed",
    "server_error": "provider server error",
    "request_error": "provider request failed",
    "client_error": "provider client could not be created",
    "fixture_missing": "vision fixture file is missing",
}


def sanitize_error(exc: BaseException) -> dict[str, Any]:
    """Map an exception to a safe category. Never copies message or headers."""
    status: Optional[int] = None
    raw_status = getattr(exc, "status_code", None)
    if isinstance(raw_status, int):
        status = raw_status
    else:
        response = getattr(exc, "response", None)
        resp_status = getattr(response, "status_code", None)
        if isinstance(resp_status, int):
            status = resp_status
    name = type(exc).__name__.lower()
    if status in (401, 403) or "auth" in name or "permission" in name:
        category = "auth_error"
    elif status == 429 or "ratelimit" in name or "rate_limit" in name:
        category = "rate_limit"
    elif "timeout" in name or status == 408:
        category = "timeout"
    elif status is not None and status >= 500:
        category = "server_error"
    elif "connection" in name:
        category = "connection_error"
    else:
        category = "request_error"
    return {
        "category": category,
        "status_code": status,
        "error_type": type(exc).__name__,
        "detail": _ERROR_DETAILS[category],
    }


def _to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            result = dump()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    extra = getattr(obj, "model_extra", None)
    return dict(extra) if isinstance(extra, dict) else {}


def _get_field(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    as_dict = _to_dict(obj)
    for name in names:
        if as_dict.get(name) is not None:
            return as_dict[name]
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        for name in names:
            if extra.get(name) is not None:
                return extra[name]
    return None


def _first_choice(obj: Any) -> Any:
    choices = _get_field(obj, "choices")
    if isinstance(choices, (list, tuple)) and choices:
        return choices[0]
    return None


def extract_usage(usage_obj: Any) -> Optional[dict[str, Any]]:
    """Return observed usage or None when unavailable (never fabricated)."""
    if usage_obj is None:
        return None
    data = _to_dict(usage_obj)
    if not data:
        # Plain objects without model_dump: read known attributes directly.
        data = {
            key: getattr(usage_obj, key, None)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        if all(v is None for v in data.values()):
            return None
    details = data.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        details = _to_dict(details)
    if not details:
        details_obj = getattr(usage_obj, "completion_tokens_details", None)
        details = _to_dict(details_obj)
        if not details and details_obj is not None:
            details = {
                "reasoning_tokens": getattr(details_obj, "reasoning_tokens", None)
            }
    usage = {
        "prompt_tokens": data.get("prompt_tokens"),
        "completion_tokens": data.get("completion_tokens"),
        "total_tokens": data.get("total_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
    }
    if all(v is None for v in usage.values()):
        return None
    return usage


def _extract_text_fields(holder: Any) -> dict[str, Any]:
    content = _get_field(holder, "content")
    reasoning = _get_field(holder, "reasoning_content", "reasoning")
    field = None
    if _get_field(holder, "reasoning_content") is not None:
        field = "reasoning_content"
    elif _get_field(holder, "reasoning") is not None:
        field = "reasoning"
    return {
        "content": content if isinstance(content, str) else None,
        "reasoning": reasoning if isinstance(reasoning, str) else None,
        "reasoning_present": isinstance(reasoning, str) and bool(reasoning),
        "reasoning_field": field,
    }


def parse_completion_response(response: Any) -> dict[str, Any]:
    choice = _first_choice(response)
    message = _get_field(choice, "message") if choice is not None else None
    fields = _extract_text_fields(message)
    return {
        **fields,
        "usage": extract_usage(_get_field(response, "usage")),
        "response_model": _get_field(response, "model"),
        "finish_reason": _get_field(choice, "finish_reason")
        if choice is not None
        else None,
    }


def parse_stream_chunks(chunks: Iterable[Any]) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: Optional[dict[str, Any]] = None
    model: Optional[str] = None
    chunk_count = 0
    content_delta_count = 0
    reasoning_field: Optional[str] = None
    finish_reason: Optional[str] = None
    for chunk in chunks:
        chunk_count += 1
        chunk_model = _get_field(chunk, "model")
        if isinstance(chunk_model, str) and chunk_model:
            model = chunk_model
        chunk_usage = extract_usage(_get_field(chunk, "usage"))
        if chunk_usage is not None:
            usage = chunk_usage
        choice = _first_choice(chunk)
        observed_finish = _get_field(choice, "finish_reason")
        if isinstance(observed_finish, str):
            finish_reason = observed_finish
        delta = _get_field(choice, "delta") if choice is not None else None
        fields = _extract_text_fields(delta)
        if fields["content"]:
            content_parts.append(fields["content"])
            content_delta_count += 1
        if fields["reasoning"]:
            reasoning_parts.append(fields["reasoning"])
        if fields["reasoning_field"]:
            reasoning_field = fields["reasoning_field"]
    reasoning_text = "".join(reasoning_parts) or None
    return {
        "content": "".join(content_parts),
        "reasoning": reasoning_text,
        "reasoning_present": bool(reasoning_text),
        "reasoning_field": reasoning_field,
        "usage": usage,
        "response_model": model,
        "chunk_count": chunk_count,
        "content_delta_count": content_delta_count,
        "finish_reason": finish_reason,
    }


def vision_answer_matches(answer: Optional[str]) -> bool:
    if not answer:
        return False
    low = answer.lower()
    blue_idx = low.find("blue")
    bottom_candidates = [
        idx for idx in (low.find("amber"), low.find("orange")) if idx >= 0
    ]
    if blue_idx < 0 or not bottom_candidates:
        return False
    return blue_idx < min(bottom_candidates)


def _truncate(text: Optional[str], limit: int = 500) -> Optional[str]:
    if text is None:
        return None
    return text[:limit]


def _encode_fixture_data_uri(fixture: Path) -> str:
    mime = "image/png" if fixture.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(fixture.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_client(api_key: str) -> Any:
    if OpenAI is None:  # pragma: no cover
        raise RuntimeError("openai package is not installed")
    return OpenAI(
        api_key=api_key,
        base_url=BASE_URL,
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
    )


def _base_result(mode: str, project_root: Path) -> dict[str, Any]:
    return {
        "task": "SETUP-002",
        "mode": mode,
        "checked_at_utc": _utc_now(),
        "model_requested": MODEL,
        "base_url": BASE_URL,
        "live_requests_made": 0,
        "config": get_config(project_root),
        "capabilities": {
            "text": {"status": "not_checked"},
            "streaming": {"status": "not_checked"},
            "vision": {
                "status": "not_checked",
                "expected": {"top": "blue", "bottom": "amber/orange"},
            },
        },
        "errors": [],
    }


def run_offline(project_root: Optional[Path] = None) -> dict[str, Any]:
    """Offline configuration check. Creates no client and no requests."""
    root = project_root or get_project_root()
    result = _base_result("offline", root)
    result["status"] = "ok"
    result["notes"] = [
        "Offline mode: zero provider requests by design.",
        "VERIFIED requires a real --live response; nothing here is verified.",
    ]
    return result


def run_live(
    project_root: Optional[Path] = None,
    client_factory: Optional[Callable[[str], Any]] = None,
    api_key: Any = _UNSET,
) -> dict[str, Any]:
    root = project_root or get_project_root()
    result = _base_result("live", root)
    key = load_api_key(root) if api_key is _UNSET else api_key
    if not _is_nonempty(key):
        result["status"] = "blocked_missing_key"
        result["blocked_code"] = BLOCKED_MISSING_KEY
        result["next_command"] = NEXT_LIVE_COMMAND
        result["notes"] = [
            "No BASETEN_API_KEY in process environment or project-root .env.",
            "Live checks were not attempted; zero provider requests made.",
        ]
        return result
    factory = client_factory or build_client
    try:
        client = factory(key)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - sanitized below
        result["status"] = "client_error"
        result["errors"] = [sanitize_error(exc)]
        return result

    counter = {"count": 0}

    def timed(fn: Callable[[], Any]) -> tuple[Any, float, Optional[dict[str, Any]]]:
        start = time.perf_counter()
        try:
            return fn(), (time.perf_counter() - start) * 1000.0, None
        except Exception as exc:  # noqa: BLE001 - sanitized below
            return None, (time.perf_counter() - start) * 1000.0, sanitize_error(exc)

    # 1/3: short text completion.
    def do_text() -> Any:
        counter["count"] += 1
        return client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": TEXT_PROMPT}],
            max_tokens=REQUEST_MAX_TOKENS,
        )

    raw, ms, err = timed(do_text)
    if err is not None:
        result["capabilities"]["text"] = {
            "status": "error", "error": err, "duration_ms": round(ms, 1),
        }
        result["errors"].append(err)
    else:
        parsed = parse_completion_response(raw)
        result["capabilities"]["text"] = {
            "status": "verified" if parsed["content"] and parsed["finish_reason"] != "length" else "incomplete",
            "content": _truncate(parsed["content"]),
            "reasoning_present": parsed["reasoning_present"],
            "reasoning_field": parsed["reasoning_field"],
            "reasoning_excerpt": _truncate(parsed["reasoning"]),
            "usage": parsed["usage"],
            "response_model": parsed["response_model"],
            "finish_reason": parsed["finish_reason"],
            "duration_ms": round(ms, 1),
        }

    # 2/3: streaming text completion.
    def do_stream() -> Any:
        counter["count"] += 1
        stream = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": STREAM_PROMPT}],
            max_tokens=REQUEST_MAX_TOKENS,
            stream=True,
            stream_options={"include_usage": True},
        )
        try:
            return parse_stream_chunks(stream)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    parsed_stream, ms, err = timed(do_stream)
    if err is not None:
        result["capabilities"]["streaming"] = {
            "status": "error", "error": err, "duration_ms": round(ms, 1),
        }
        result["errors"].append(err)
    else:
        result["capabilities"]["streaming"] = {
            "status": "verified"
            if parsed_stream["content"] and parsed_stream["finish_reason"] != "length"
            else "incomplete",
            "content": _truncate(parsed_stream["content"]),
            "reasoning_present": parsed_stream["reasoning_present"],
            "reasoning_field": parsed_stream["reasoning_field"],
            "reasoning_excerpt": _truncate(parsed_stream["reasoning"]),
            "usage": parsed_stream["usage"],
            "response_model": parsed_stream["response_model"],
            "chunk_count": parsed_stream["chunk_count"],
            "content_delta_count": parsed_stream["content_delta_count"],
            "finish_reason": parsed_stream["finish_reason"],
            "duration_ms": round(ms, 1),
        }

    # 3/3: vision completion with the provided fixture.
    fixture = root / FIXTURE_RELATIVE
    expected = {"top": "blue", "bottom": "amber/orange"}
    if not fixture.is_file():
        result["capabilities"]["vision"] = {
            "status": "unavailable_fixture_missing",
            "expected": expected,
            "actual_answer": None,
            "matches_expected": None,
            "error": {
                "category": "fixture_missing",
                "status_code": None,
                "error_type": "FileNotFoundError",
                "detail": _ERROR_DETAILS["fixture_missing"],
            },
        }
    else:

        def do_vision() -> Any:
            counter["count"] += 1
            data_uri = _encode_fixture_data_uri(fixture)
            return client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": VISION_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": data_uri, "detail": "auto"},
                            },
                        ],
                    }
                ],
                max_tokens=REQUEST_MAX_TOKENS,
            )

        raw, ms, err = timed(do_vision)
        if err is not None:
            result["capabilities"]["vision"] = {
                "status": "error",
                "expected": expected,
                "actual_answer": None,
                "matches_expected": None,
                "error": err,
                "duration_ms": round(ms, 1),
            }
            result["errors"].append(err)
        else:
            parsed = parse_completion_response(raw)
            result["capabilities"]["vision"] = {
                "status": "verified" if vision_answer_matches(parsed["content"]) and parsed["finish_reason"] != "length" else "answer_mismatch_or_incomplete",
                "expected": expected,
                "actual_answer": _truncate(parsed["content"]),
                "matches_expected": vision_answer_matches(parsed["content"]),
                "reasoning_present": parsed["reasoning_present"],
                "reasoning_field": parsed["reasoning_field"],
                "reasoning_excerpt": _truncate(parsed["reasoning"]),
                "usage": parsed["usage"],
                "response_model": parsed["response_model"],
                "finish_reason": parsed["finish_reason"],
                "duration_ms": round(ms, 1),
            }

    result["live_requests_made"] = counter["count"]
    result["status"] = (
        "completed" if all(c["status"] == "verified" for c in result["capabilities"].values())
        else "partial"
    )
    close_client = getattr(client, "close", None)
    if callable(close_client):
        close_client()
    result["notes"] = [
        "Live mode: at most three requests, retries disabled, no fallback model.",
        "VERIFIED above means observed in a real provider response.",
    ]
    return result


def write_capabilities(result: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return output_path


def main(
    argv: Optional[list[str]] = None,
    project_root: Optional[Path] = None,
    client_factory: Optional[Callable[[str], Any]] = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Baseten capability preflight (offline by default)."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="enable live provider checks (at most three requests)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="capabilities JSON path (default: .local/preflight/baseten-capabilities.json)",
    )
    args = parser.parse_args(argv)
    root = project_root or get_project_root()
    output = args.output or (root / DEFAULT_OUTPUT_RELATIVE)
    if not output.is_absolute():
        output = root / output
    result = (
        run_live(root, client_factory=client_factory)
        if args.live
        else run_offline(root)
    )
    write_capabilities(result, output)
    summary = {
        "mode": result["mode"],
        "status": result["status"],
        "live_requests_made": result["live_requests_made"],
        "output": str(output),
    }
    if result.get("blocked_code"):
        summary["blocked_code"] = result["blocked_code"]
        summary["next_command"] = result["next_command"]
    print(json.dumps(summary, ensure_ascii=False))
    if result["status"] == "blocked_missing_key":
        return 2
    if result["status"] not in ("ok", "completed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
