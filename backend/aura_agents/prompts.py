"""Deterministic prompts, bounded evidence packets and strict parsing.

Model output is never trusted for metadata: parsers accept only the
documented JSON keys and validate identity, completeness and evidence
references. Anything malformed, truncated or unknown fails closed with
no repair loop.
"""
from __future__ import annotations

import json
from typing import Any

from aura_api.orchestration import EvidenceBundle

from .adapter import ProviderOutputError

MAX_PACKET_CHARS = 12000
MAX_FINDING_CHARS = 500

SPECIALIST_TASKS = {
    "logs": "Inspect job counters and actual diagnostic log lines for "
            "failures or rejected work.",
    "memory": "Inspect measured RSS versus baseline/threshold and the "
              "retained cache size.",
    "config": "Inspect the actual worker cache configuration, especially "
              "whether eviction is enabled.",
    "vision": "Inspect the supplied measured chart image together with "
              "current telemetry.",
}

_SYSTEM = (
    "You are one specialist in a bounded incident-diagnosis swarm. "
    "Use only the supplied evidence. Never request tools, shell commands "
    "or code execution. Reply with ONLY one JSON object with keys "
    "agent_id, status, summary, findings, evidence_refs. agent_id must "
    "match the requested specialist. status must be completed only when "
    "the evidence supports nonempty findings; otherwise incomplete. "
    "evidence_refs may contain only IDs present in the evidence packet."
    " Exact JSON types: agent_id, status and summary are strings; "
    "findings is a nonempty array of plain strings, NEVER objects or a dictionary; "
    "evidence_refs is an array of strings. Use 1-4 concise findings. "
    'Shape: {"agent_id":"<requested specialist>","status":"completed",'
    '"summary":"<evidence-based summary>","findings":["<finding>"],'
    '"evidence_refs":["<supplied evidence ID>"]}. No Markdown fences.'
)


def _current_telemetry(evidence: EvidenceBundle) -> dict:
    return {
        "run_id": evidence.run_id, "scenario": evidence.scenario,
        "worker_generation": evidence.worker_generation,
        "measured_at": evidence.measured_at,
        "baseline_rss_bytes": evidence.baseline_rss_bytes,
        "rss_bytes": evidence.rss_bytes, "rss_fresh": evidence.rss_fresh,
        "rss_threshold_bytes": evidence.rss_threshold_bytes,
        "retained_cache_bytes": evidence.retained_cache_bytes,
        "healthy_cache_bytes": evidence.healthy_cache_bytes,
        "ceiling_bytes": evidence.ceiling_bytes,
        "completed_jobs": evidence.completed_jobs,
        "failed_jobs": evidence.failed_jobs,
        "worker_state": evidence.worker_state,
    }


def build_specialist_packet(evidence: EvidenceBundle, agent_id: str) -> dict:
    """Bounded per-specialist evidence; keys are the citable evidence IDs."""
    current = _current_telemetry(evidence)
    recent = list(evidence.sampled_telemetry
                  or evidence.recent_measurements)[-8:]
    if agent_id == "logs":
        logs: Any = [
            {"source": entry.get("source", "worker_stderr"),
             "text": str(entry.get("text", ""))[:300]}
            for entry in evidence.diagnostic_logs[-10:]
        ]
        if not logs:
            # Explicitly derived from counters; NOT verbatim worker logs.
            logs = {
                "source": "derived_from_job_counters", "verbatim": False,
                "note": "No per-request worker logs exist for this run; "
                        "this summary is derived from job counters only.",
                "completed_jobs": evidence.completed_jobs,
                "failed_jobs": evidence.failed_jobs,
            }
        return {"telemetry.current": current, "diagnostic.logs": logs,
                "telemetry.recent": recent}
    if agent_id == "memory":
        return {"telemetry.current": current, "telemetry.recent": recent,
                "worker.status": {
                    "state": evidence.worker_state,
                    "status_fresh": evidence.status_fresh,
                    "status_timestamp": evidence.status_timestamp}}
    if agent_id == "config":
        return {"worker.config": {
                    "source": evidence.worker_config_source,
                    **evidence.worker_config},
                "worker.status": {
                    "state": evidence.worker_state,
                    "eviction_enabled": evidence.eviction_enabled,
                    "status_fresh": evidence.status_fresh},
                "telemetry.current": current}
    if agent_id == "vision":
        return {"telemetry.current": current,
                "chart.measured": {
                    "source": "codex_owned_chart_renderer",
                    "note": "Chart image is attached separately as an "
                            "image input; it is measured worker data."}}
    raise ValueError(f"unknown specialist: {agent_id}")


_MEASUREMENT_KEYS = (
    "timestamp", "monotonic", "rss_bytes", "retained_cache_bytes",
    "completed_jobs", "failed_jobs", "rss_fresh", "status_fresh",
    "generation", "state")


def _trim_measurements(rows) -> list[dict]:
    trimmed = []
    for row in list(rows)[-8:]:
        if isinstance(row, dict):
            trimmed.append({
                key: row.get(key) for key in _MEASUREMENT_KEYS
                if key in row})
        else:
            trimmed.append({"value": str(row)[:200]})
    return trimmed


def _trim_packet(packet: dict, recent_limit: int) -> dict:
    """Structurally trim lists/strings; never slice serialized JSON."""
    trimmed: dict = {}
    for key, value in packet.items():
        if key == "telemetry.recent":
            trimmed[key] = _trim_measurements(value)[-recent_limit:]
        elif key == "diagnostic.logs" and isinstance(value, list):
            trimmed[key] = [
                {"source": str(entry.get("source", "worker_stderr"))[:64],
                 "text": str(entry.get("text", ""))[:300]}
                for entry in value[-10:] if isinstance(entry, dict)
            ]
        elif key == "findings" and isinstance(value, list):
            trimmed[key] = [
                {"agent_id": entry.get("agent_id"),
                 "summary": str(entry.get("summary", ""))[:500],
                 "findings": [str(item)[:MAX_FINDING_CHARS]
                              for item in entry.get("findings", [])[:8]],
                 "evidence_refs": list(entry.get("evidence_refs", []))[:16]}
                for entry in value[:4] if isinstance(entry, dict)
            ]
        elif isinstance(value, str):
            trimmed[key] = value[:1000]
        else:
            trimmed[key] = value
    return trimmed


def _bounded_packet_text(packet: dict) -> str:
    """Serialize a structurally trimmed packet as valid JSON, or fail."""
    for recent_limit in (8, 4, 2, 0):
        trimmed = _trim_packet(packet, recent_limit)
        if recent_limit == 0:
            trimmed.pop("telemetry.recent", None)
        text = json.dumps(trimmed, separators=(",", ":"), default=str)
        if len(text) <= MAX_PACKET_CHARS:
            return text
    raise ProviderOutputError("evidence_too_large")


def build_specialist_messages(
    evidence: EvidenceBundle, agent_id: str,
) -> tuple[list[dict], set[str]]:
    packet = build_specialist_packet(evidence, agent_id)
    user = (
        f"SPECIALIST: {agent_id}\nTASK: {SPECIALIST_TASKS[agent_id]}\n"
        f"EVIDENCE PACKET (JSON): {_bounded_packet_text(packet)}"
    )
    return ([{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": user}], set(packet))


def build_synthesis_messages(
    evidence: EvidenceBundle, findings: list,
) -> tuple[list[dict], set[str]]:
    packet = {
        "telemetry.current": _current_telemetry(evidence),
        "findings": [
            {"agent_id": finding.agent_id, "summary": finding.summary,
             "findings": list(finding.findings)[:8],
             "evidence_refs": list(finding.evidence_refs)}
            for finding in findings
        ],
    }
    # Only evidence actually supplied in this packet may be cited: the
    # current telemetry, the four findings and the refs inside them.
    allowed_refs = {"telemetry.current"} | {
        f"finding:{finding.agent_id}" for finding in findings} | {
        ref for finding in findings for ref in finding.evidence_refs}
    system = (
        "You are the synthesis step of a bounded incident-diagnosis "
        "swarm. Use only the supplied specialist findings and telemetry. "
        "Reply with ONLY one JSON object with keys action, reason, "
        "evidence_refs. action must be restore_cache_policy only when "
        "all findings support restoring the cache policy; otherwise "
        "abstain. Never propose any other action, tool, shell command "
        "or code."
    )
    user = (
        "SPECIALIST: synthesis\n"
        f"ALLOWED EVIDENCE REFS: {json.dumps(sorted(allowed_refs))}\n"
        f"EVIDENCE PACKET (JSON): {_bounded_packet_text(packet)}"
    )
    return ([{"role": "system", "content": system},
             {"role": "user", "content": user}], allowed_refs)


def _load_json_object(content, finish_reason) -> dict:
    if finish_reason == "length":
        raise ProviderOutputError("truncated_output")
    if finish_reason != "stop":
        raise ProviderOutputError("incomplete_finish")
    if content is None or not content.strip():
        raise ProviderOutputError("empty_response")
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        raise ProviderOutputError("malformed_json") from None
    if not isinstance(data, dict):
        raise ProviderOutputError("malformed_json")
    return data


def _validate_refs(value, allowed_refs: set[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise ProviderOutputError("invalid_evidence_refs")
    refs = []
    for item in value:
        if not isinstance(item, str) or item not in allowed_refs:
            raise ProviderOutputError("invalid_evidence_refs")
        refs.append(item)
    return refs


def _validate_text_list(value, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 32 \
            or (not value and not allow_empty):
        raise ProviderOutputError("invalid_findings")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ProviderOutputError("invalid_findings")
        items.append(item[:MAX_FINDING_CHARS])
    return items


def parse_specialist_output(content, finish_reason, expected_agent: str,
                            allowed_refs: set[str]) -> dict:
    data = _load_json_object(content, finish_reason)
    if set(data) - {"agent_id", "status", "summary", "findings",
                    "evidence_refs"}:
        raise ProviderOutputError("unexpected_output_keys")
    if data.get("agent_id") != expected_agent:
        raise ProviderOutputError("agent_mismatch")
    if data.get("status") != "completed":
        raise ProviderOutputError("incomplete_output")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ProviderOutputError("invalid_summary")
    return {
        "summary": summary[:2000],
        "findings": _validate_text_list(data.get("findings")),
        "evidence_refs": _validate_refs(data.get("evidence_refs"),
                                        allowed_refs),
    }


def parse_synthesis_output(content, finish_reason,
                           allowed_refs: set[str]) -> dict:
    data = _load_json_object(content, finish_reason)
    if set(data) - {"action", "reason", "evidence_refs"}:
        raise ProviderOutputError("unexpected_output_keys")
    action = data.get("action")
    if action not in ("restore_cache_policy", "abstain"):
        raise ProviderOutputError("unknown_action")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ProviderOutputError("invalid_reason")
    return {
        "action": action, "reason": reason[:2000],
        "evidence_refs": _validate_refs(data.get("evidence_refs"),
                                        allowed_refs),
    }
