"""Evidence-bound operator review of unknown operation outcomes.

Reconciliation acknowledges uncertainty after a human inspects the actual
target; it never proves the old command succeeded, availability recovered,
or a defect was fixed. Snapshot stability DOES NOT prove an earlier Docker
command has stopped: containers may still be restarting or settling while
two reads look identical. Confirmation therefore requires an explicit
operator attestation bound to the exact reviewed record.

No function here calls a model, mutates a target, adopts a baseline, or
retries an old action. All evidence is selected allowlist fields, redacted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import datetime, timedelta, timezone

from .actions import _fresh, _hash, _identity, _spec_hash
from .models import OpsError, TargetSpec, utc_now
from .redaction import redact_data
from .store import proposal_content_hash


REVIEW_TTL_SECONDS = 300
OPERATOR_NOTE_MAX = 500
CONFIRM_COLLECT_TIMEOUT = 60.0

ATTESTATION_TEXT = (
    "I have inspected the displayed target state and confirm "
    "the previous operation has ended."
)

EXPLAINS_OBSERVE_ONLY = (
    "Confirmation expires old pending proposals for this target and resets "
    "its policy to observe-only. No old action is retried; future "
    "intervention needs a new investigation, proposal and approval."
)

EXPLAINS_NO_PROOF = (
    "Stable snapshots DO NOT prove the earlier Docker command has stopped. "
    "This review only records inspected target state; it is not a success, "
    "availability or defect-fix claim."
)

_IDENTITY_KEYS = ("container_id", "image_id", "image_ref", "started_at")
_CONFIG_KEYS = ("memory_limit_bytes", "published_ports", "server_port")


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise OpsError("invalid_action_data", "Evidence is not valid JSON.") from exc


def review_binding_hash(binding: dict) -> str:
    return hashlib.sha256(_canonical(binding)).hexdigest()


def select_probes(observation: dict) -> list[dict]:
    probes = observation.get("probes")
    if not isinstance(probes, list):
        raise OpsError("observation_incomplete", "Timestamped probe results are required.", 409)
    selected = []
    for probe in probes:
        if not isinstance(probe, dict) or not isinstance(probe.get("url"), str) \
                or not isinstance(probe.get("ok"), bool) \
                or not isinstance(probe.get("status_code"), int):
            raise OpsError("observation_incomplete", "Timestamped probe results are required.", 409)
        selected.append({"url": probe["url"], "ok": probe["ok"], "status_code": probe["status_code"]})
    return sorted(selected, key=lambda item: item["url"])


def select_stable(observation: dict) -> dict:
    """Explicit stable selection; volatile fields are never compared.

    Compared: identity (4 keys), runtime running/status, selected config
    (memory limit, published ports, server port), probe url/ok/status and
    the healthy boolean. Ignored: timestamps, CPU/memory samples, log tails,
    durations, restart counts and exit codes.
    """
    runtime = observation.get("runtime") or {}
    if not isinstance(runtime.get("running"), bool) or not isinstance(runtime.get("status"), str):
        raise OpsError("observation_incomplete", "Complete runtime state is required.", 409)
    config = observation.get("config") or {}
    if not isinstance(config, dict) or any(key not in config for key in _CONFIG_KEYS) \
            or type(config.get("memory_limit_bytes")) is not int or config["memory_limit_bytes"] < 0 \
            or not isinstance(config.get("published_ports"), dict):
        raise OpsError("observation_incomplete", "Complete configuration state is required.", 409)
    if type(observation.get("healthy")) is not bool:
        raise OpsError("observation_incomplete", "A definitive health boolean is required.", 409)
    return {"identity": _identity(observation),
            "runtime": {"running": runtime["running"], "status": runtime["status"]},
            "config": {key: config.get(key) for key in _CONFIG_KEYS},
            "probes": select_probes(observation),
            "healthy": observation["healthy"]}


def check_complete_observation(spec: TargetSpec, observation: dict) -> None:
    """Fresh, complete read-only evidence; unhealthy HTTP is valid evidence."""
    _fresh(observation)
    error = observation.get("error")
    if isinstance(error, dict) and error.get("code"):
        raise OpsError(str(error["code"]), str(error.get("message") or "Target evidence unavailable."), 503)
    if observation.get("collection_errors"):
        raise OpsError("observation_incomplete",
                       "Partial or failed collection cannot support reconciliation.", 409)
    select_stable(observation)
    if observation["runtime"]["running"]:
        resources = observation.get("resources") or {}
        for key in ("memory_bytes", "memory_limit_bytes"):
            value = resources.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise OpsError("observation_incomplete", "Complete memory measurements are required.", 409)
    expected_urls = sorted(probe.url for probe in spec.probes)
    if [probe["url"] for probe in select_probes(observation)] != expected_urls:
        raise OpsError("observation_incomplete", "Probe results must cover every enrolled probe.", 409)


def require_stable_pair(first: dict, second: dict) -> dict:
    """Two fresh observations must agree on identity and selected config."""
    stable_first, stable_second = select_stable(first), select_stable(second)
    if stable_first["identity"] != stable_second["identity"] \
            or stable_first["runtime"] != stable_second["runtime"]:
        raise OpsError("target_changed", "Target identity changed between review observations.", 409)
    if stable_first["config"] != stable_second["config"]:
        raise OpsError("config_changed", "Target configuration changed between review observations.", 409)
    if stable_first["probes"] != stable_second["probes"] \
            or stable_first["healthy"] != stable_second["healthy"]:
        raise OpsError("reconciliation_probes_changed",
                       "Probe results changed between review observations.", 409)
    return stable_first


def allowed_images(unknowns: list[dict]) -> set[str]:
    """Exact image IDs already bound to these operations (never new ones)."""
    allowed: set[str] = set()
    for proposal in unknowns:
        candidates = [proposal.get("base_image_id"), proposal.get("candidate_image_id"),
                      (proposal.get("expected_identity") or {}).get("image_id"),
                      ((proposal.get("result") or {}).get("identity") or {}).get("image_id"),
                      (proposal.get("deployment_identity") or {}).get("image_id"),
                      (proposal.get("rollback_identity") or {}).get("image_id")]
        for image_id in candidates:
            if isinstance(image_id, str) and image_id.startswith("sha256:"):
                allowed.add(image_id)
    return allowed


def require_recognized_image(unknowns: list[dict], identity: dict) -> None:
    if identity.get("image_id") not in allowed_images(unknowns):
        raise OpsError("reconciliation_image_unrecognized",
                       "Current image is not bound to the reviewed operations; "
                       "it needs new enrollment, not reconciliation.", 409)


def unknown_entries(unknowns: list[dict]) -> list[dict]:
    entries = [{"proposal_id": item.get("proposal_id"), "action": item.get("action"),
                "content_hash": proposal_content_hash(item)} for item in unknowns]
    return sorted(entries, key=lambda item: str(item.get("proposal_id")))


def build_review(*, review_id: str, target_record: dict, spec: TargetSpec,
                 unknowns: list[dict], first: dict, second: dict) -> dict:
    """Bind target version, full spec, endpoint, identity, config, probes and
    the exact set/content hashes of ALL unresolved unknowns for this target."""
    stable = require_stable_pair(first, second)
    require_recognized_image(unknowns, stable["identity"])
    created_at = utc_now()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=REVIEW_TTL_SECONDS)).isoformat()
    entries = unknown_entries(unknowns)
    binding = {"review_id": review_id, "target_id": spec.target_id,
               "target_created_at": target_record.get("created_at"),
               "spec_hash": _spec_hash(spec),
               "policy_hash": _hash(spec.policy.model_dump(mode="json")),
               "docker_context": spec.docker_context, "container_name": spec.container_name,
               "identity": stable["identity"], "runtime": stable["runtime"],
               "config": stable["config"], "probes": stable["probes"], "healthy": stable["healthy"],
               "unknowns": entries, "expires_at": expires_at}
    review = {"review_id": review_id, "target_id": spec.target_id, "status": "pending",
              "created_at": created_at, "expires_at": expires_at,
              "target_created_at": target_record.get("created_at"),
              "spec": spec.model_dump(mode="json"),
              "spec_hash": binding["spec_hash"], "policy_hash": binding["policy_hash"],
              "docker_context": spec.docker_context, "container_name": spec.container_name,
              "identity": stable["identity"], "runtime": stable["runtime"],
              "config": stable["config"], "probes": stable["probes"], "healthy": stable["healthy"],
              "observations": [{"timestamp": first.get("timestamp"), "healthy": first.get("healthy")},
                               {"timestamp": second.get("timestamp"), "healthy": second.get("healthy")}],
              "unknowns": entries,
              "notices": {"no_proof": EXPLAINS_NO_PROOF, "observe_only": EXPLAINS_OBSERVE_ONLY},
              "review_hash": review_binding_hash(binding)}
    return redact_data(review)


def check_attestation(attestation: str) -> None:
    if attestation != ATTESTATION_TEXT:
        raise OpsError("attestation_invalid",
                       "Confirmation requires the exact operator attestation statement.", 400)


def check_operator_note(note: str) -> str:
    if not isinstance(note, str) or not note.strip() or len(note) > OPERATOR_NOTE_MAX:
        raise OpsError("invalid_operator_note", "A bounded nonempty operator note is required.")
    return note.strip()


def hashes_match(expected: str, supplied: str) -> bool:
    return isinstance(supplied, str) and hmac.compare_digest(expected, supplied)
