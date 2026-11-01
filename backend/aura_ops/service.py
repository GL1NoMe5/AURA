"""Operations lifecycle. AURA owns its jobs, never enrolled applications."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from aura_agents.adapter import BasetenAdapter, ProviderCallError, ProviderOutputError
from .docker import DockerTarget, runtime_info
from .evidence_quality import (check_collection, check_probes, check_resources,
                               check_runtime, pressure_ok)
from .investigator import OpsInvestigator
from .models import ActionKind, OpsError, Policy, TargetSpec, utc_now
from .reconciliation import (CONFIRM_COLLECT_TIMEOUT, build_review,
                             check_attestation, check_complete_observation, check_operator_note,
                             hashes_match, require_recognized_image, require_stable_pair,
                             select_stable)
from .redaction import redact_data
from .store import OpsStore, proposal_content_hash
from .target_management import (build_configuration, check_target_generation,
                                connection_revision, investigation_revision)

ACTIVE = {"queued", "investigating", "awaiting_approval", "executing", "verifying", "preparing_patch", "preparing_deployment"}

# prepare_patch starts only from human-reviewable states with a diagnosis.
# Completed (verified_fixed/mitigated), already-tested fix_proposed and any
# active work (including awaiting_approval) are never overwritten.
PREPARE_PATCH_ALLOWED_PHASES = frozenset({"needs_human", "cancelled"})


def _is_definitely_never_dispatched(stored, target_id: str) -> bool:
    """Positive evidence of never-dispatch; ambiguous legacy stays unknown.

    Safe cancellation requires status proposed/executing, matching target,
    no dispatch evidence (attempt timestamps, dispatched flags, deployment
    or rollback identities, completed result) and, for executing, an explicit
    policy/user authorization. Missing target/status/authorization or any
    dispatch evidence is conservatively NOT safe: absence of one field in an
    ambiguous legacy record never proves the command never ran.
    """
    if not isinstance(stored, dict) or stored.get("target_id") != target_id:
        return False
    if stored.get("status") not in ("proposed", "executing"):
        return False
    if stored.get("dispatch_attempted_at") or stored.get("deployment_dispatched") \
            or stored.get("rollback_attempted_at") or stored.get("deployment_identity") \
            or stored.get("rollback_identity"):
        return False
    result = stored.get("result") or {}
    if isinstance(result, dict) and result.get("command_completed"):
        return False
    if stored.get("status") == "executing" and stored.get("authorization") not in ("policy", "user"):
        return False
    return True


class OpsService:
    def __init__(self, root: Path, *, store=None, docker_factory=DockerTarget,
                 investigator=None, executor=None, patch_engine=None, deployment_manager=None, functional_verifier=None, interval=3.0,
                 verification_seconds=15.0, verification_timeout=60.0,
                 reconciliation_timeout=CONFIRM_COLLECT_TIMEOUT):
        self.root = Path(root)
        from .credentials import load_probe_credentials
        load_probe_credentials(self.root)
        self.store = store or OpsStore(self.root / ".local" / "ops")
        self.docker_factory = docker_factory
        self.investigator = investigator or OpsInvestigator(BasetenAdapter(project_root=self.root))
        from .actions import ActionExecutor
        self.executor = executor or ActionExecutor(self.store, docker_factory=docker_factory)
        self.patch_engine = patch_engine
        from .deployment import DeploymentManager
        from .verification import verify_functional
        self.deployment_manager = deployment_manager or DeploymentManager(self.store, docker_factory=docker_factory)
        self.functional_verifier = functional_verifier or verify_functional
        self.interval = interval
        self.verification_seconds, self.verification_timeout = verification_seconds, verification_timeout
        self.reconciliation_timeout = reconciliation_timeout
        self.observations = {}
        self.histories = {}
        self.bad_streak = {}
        self.docker_status = {"available": False, "message": "Checking Docker availability."}
        self._subscribers = set()
        self._monitor_task = None
        self._task = None
        self._cancel = None
        self._active_id = None
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def provider_ready(self):
        return self.investigator.ready

    def target(self, target_id):
        row = self.store.get_target(target_id)
        if row is None:
            raise OpsError("target_not_found", "Application is not enrolled.", 404)
        return TargetSpec.model_validate({k: v for k, v in row.items() if k in TargetSpec.model_fields})

    def investigation(self, investigation_id):
        row = self.store.get_investigation(investigation_id)
        if row is None:
            raise OpsError("investigation_not_found", "Investigation was not found.", 404)
        return row

    def _target_record(self, target_id):
        row = self.store.get_target(target_id)
        if row is None:
            raise OpsError("target_not_found", "Application is not enrolled.", 404)
        return row

    def target_view(self, target_id):
        return self._target_view(self._target_record(target_id))

    def _target_view(self, row):
        target_id = row["target_id"]
        unknowns = self.store.list_unresolved_unknowns(target_id)
        snapshot = self.store.get_setting(f"snapshot.{target_id}")
        return {**row, "connection_revision": connection_revision(row),
                "configuration": build_configuration(row, snapshot),
                "observation": self.observations.get(target_id),
                "reconciliation_required": bool(unknowns),
                "unresolved_unknown_ids": [item.get("proposal_id") for item in unknowns]}

    def _check_generation(self, row):
        check_target_generation(self.store, row["target_id"], row["investigation_id"])

    def _mutation_blocked(self, target_id):
        """Live + durable busy/unknown gate for profile update/removal."""
        if self.store.list_unresolved_unknowns(target_id):
            raise OpsError("target_reconciliation_required",
                           "A previous operation outcome is unknown; reconcile the target first.", 409)
        if self._target_has_active_work(target_id):
            raise OpsError("operation_busy", "Finish or decide the pending operation for this target first.", 409)
        inv_phase = {item["investigation_id"]: item.get("phase")
                     for item in self.store.scan_investigations(target_id)}
        for proposal in self.store.list_proposals():
            if proposal.get("target_id") != target_id:
                continue
            if proposal.get("status") in {"executing", "dispatching", "verifying", "rolling_back"}:
                raise OpsError("operation_busy", "Finish or decide the pending operation for this target first.", 409)
            if proposal.get("status") == "proposed" \
                    and inv_phase.get(proposal.get("investigation_id")) == "awaiting_approval":
                raise OpsError("operation_busy", "Finish or decide the pending operation for this target first.", 409)

    def _proposal_shows_dispatch(self, proposal: dict) -> bool:
        if not isinstance(proposal, dict):
            return False
        if proposal.get("status") in ("executing", "dispatching", "verifying",
                                      "rolling_back", "executed", "verified",
                                      "verification_failed", "rolled_back", "unknown"):
            return True
        if proposal.get("dispatch_attempted_at") or proposal.get("deployment_dispatched") \
                or proposal.get("rollback_attempted_at") or proposal.get("deployment_identity") \
                or proposal.get("rollback_identity"):
            return True
        result = proposal.get("result") or {}
        return isinstance(result, dict) and bool(result.get("command_completed"))

    def _startup_rebuild_check(self, row: dict, *, allow_retry=False) -> tuple[bool, str | None]:
        """DB-only restart repair for retained tested patches. No Docker/model calls.

        Returns (rebuildable, reason). Awaiting_approval with a never-dispatched
        deploy proposal, or preparing_deployment interrupted before dispatch,
        may return to fix_proposed when bindings are consistent and no
        dispatch/unknown evidence exists. Anything else stays needs_human.
        """
        phase = row.get("phase")
        allowed = {"awaiting_approval", "preparing_deployment"}
        if allow_retry:
            allowed.update(("needs_human", "cancelled"))
        if phase not in allowed:
            return False, None
        patch = row.get("patch") or {}
        if not isinstance(patch, dict) or patch.get("status") != "tests_passed" \
                or not patch.get("patch_id"):
            return False, "No retained successfully tested patch was found."
        if "target_id" in patch and patch.get("target_id") != row.get("target_id"):
            return False, "Retained patch does not belong to this target."
        if "investigation_id" in patch \
                and patch.get("investigation_id") != row.get("investigation_id"):
            return False, "Retained patch does not belong to this investigation."
        target_record = self.store.get_target(row.get("target_id") or "")
        if target_record is None:
            return False, "Target enrollment is missing."
        if investigation_revision(row) != connection_revision(target_record):
            return False, "Target configuration changed since this diagnosis."
        enrolled_revision = target_record.get("source_revision")
        if "source_revision" in patch \
                and patch.get("source_revision") != enrolled_revision:
            return False, "Enrolled source changed since the patch was tested."
        if row.get("source_revision") is not None \
                and row.get("source_revision") != enrolled_revision:
            return False, "Enrolled source changed since diagnosis."
        if "source_revision" in patch and row.get("source_revision") is not None \
                and patch.get("source_revision") != row.get("source_revision"):
            return False, "Patch source does not match the diagnosis source."
        if self.store.list_unresolved_unknowns(row["target_id"]):
            return False, "Unresolved unknown outcome requires reconciliation first."
        proposals = self.store.list_proposals(row["investigation_id"])
        if any(self._proposal_shows_dispatch(item) for item in proposals):
            return False, "Dispatch evidence exists; review actual state before retrying."
        if phase == "awaiting_approval":
            proposal = self.store.get_proposal(row["proposal_id"]) if row.get("proposal_id") else None
            if proposal is None or proposal.get("status") != "proposed" \
                    or proposal.get("action") != "deploy_verified_patch" \
                    or proposal.get("investigation_id") != row["investigation_id"] \
                    or proposal.get("target_id") != row["target_id"] \
                    or proposal.get("patch_id") != patch.get("patch_id"):
                return False, "No matching never-dispatched deployment proposal was found."
        return True, None

    def _persisted_verified_fix(self, row: dict) -> dict | None:
        """Recover the result committed just before phase(verified_fixed).

        This copies previously recorded verification, not a new health check or
        permission to promote. Promotion still revalidates the live deployment.
        """
        if row.get("phase") not in ("executing", "verifying") or not row.get("proposal_id"):
            return None
        proposal = self.store.get_proposal(row["proposal_id"])
        patch = row.get("patch") or {}
        if not proposal or not isinstance(patch, dict) or not patch.get("patch_id") \
                or proposal.get("status") != "verified" \
                or proposal.get("action") != ActionKind.DEPLOY.value \
                or proposal.get("target_id") != row["target_id"] \
                or proposal.get("investigation_id") != row["investigation_id"] \
                or proposal.get("patch_id") != patch["patch_id"]:
            return None
        verification = proposal.get("verification")
        if not isinstance(verification, dict) \
                or verification.get("verified") is not True \
                or verification.get("root_cause_fixed") is not True \
                or verification.get("kind") != "tested_patch_and_runtime" \
                or not isinstance(verification.get("availability"), dict) \
                or verification["availability"].get("verified") is not True \
                or not isinstance(verification.get("functional"), dict) \
                or verification["functional"].get("verified") is not True:
            return None
        return verification

    def _capture_blocked(self, target_id: str) -> bool:
        """Same-target pending approval or in-flight work, unpaginated."""
        if self._target_has_active_work(target_id):
            return True
        for proposal in self.store.list_proposals():
            if proposal.get("target_id") == target_id \
                    and proposal.get("status") == "proposed":
                return True
        return False

    async def startup(self):
        # A restart invalidates unconfirmed reviews; confirmed audit records
        # survive and keep covering their exact reconciled content.
        # No Docker, model, mutation, patch or rollback work runs here.
        self.store.expire_pending_reviews()
        # Unpaginated safety scan; UI windows must never decide repair.
        all_rows = self.store.scan_investigations()
        original_phase = {row["investigation_id"]: row.get("phase") for row in all_rows}
        # Persist unknown gates BEFORE rewriting investigation phases. If this
        # startup itself is interrupted, the next startup can still classify
        # every unprocessed executed proposal from its original active phase.
        for proposal in self.store.list_proposals():
            status = proposal.get("status")
            if status in ("executing", "dispatching", "verifying", "rolling_back"):
                self.store.update_proposal(proposal["proposal_id"], status="unknown", error="Execution was interrupted; reconcile actual state before retrying.")
            elif status == "executed":
                inv_id = proposal.get("investigation_id")
                if original_phase.get(inv_id) in ("executing", "verifying") \
                        and proposal.get("verification") is None:
                    inv = self.store.get_investigation(inv_id) if inv_id else None
                    if inv is None or inv.get("verification") is None:
                        self.store.update_proposal(
                            proposal["proposal_id"], status="unknown",
                            error="Verification was interrupted before a result was recorded; reconcile actual state before retrying.")
        for row in all_rows:
            if row.get("phase") in ACTIVE:
                verified = self._persisted_verified_fix(row)
                if verified is not None:
                    self.store.update_investigation(
                        row["investigation_id"], phase="verified_fixed",
                        verification=verified, error=None,
                        baseline_error=row.get("baseline_error") or {
                            "code": "promotion_interrupted",
                            "message": "Recorded verified fix retained. Review and retry saving its version baseline."})
                    continue
                rebuildable, reason = self._startup_rebuild_check(row)
                if rebuildable:
                    error = {"code": "interrupted",
                             "message": "AURA stopped during deployment preparation. "
                                        "Tested fix retained; request prepare-deployment to rebuild "
                                        "a fresh proposal for review."}
                    self.store.update_investigation(
                        row["investigation_id"], phase="fix_proposed", error=error)
                else:
                    message = ("AURA stopped during this investigation. "
                               "Review the current application state before retrying.")
                    if reason:
                        message = f"{reason} {message}"
                    self.store.update_investigation(
                        row["investigation_id"], phase="needs_human",
                        error={"code": "interrupted", "message": message})
        for proposal in self.store.list_proposals():
            if proposal.get("status") == "proposed":
                self.store.update_proposal(proposal["proposal_id"], status="expired")
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor())

    async def shutdown(self):
        self._closed = True
        if self._cancel:
            self._cancel.set()
        tasks = [task for task in (self._monitor_task, self._task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.investigator.close()
        self.store.close()

    async def emit(self, kind, payload, target_id=None, investigation_id=None):
        event = self.store.append_event(kind, redact_data(payload), target_id, investigation_id)
        for queue in tuple(self._subscribers):
            if queue.full():
                # A gap is visible in seq; the client reloads an authoritative snapshot.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)
        return event

    def subscribe(self):
        queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)

    def snapshot(self):
        return {"seq": self.store.latest_seq(), "provider_ready": self.provider_ready,
                "docker": self.docker_status, "targets": self.list_targets(),
                "investigations": self.store.list_investigations(limit=50),
                "proposals": self.store.list_proposals(), "active_id": self._active_id,
                "capabilities": {"max_targets": 3, "external_targets_survive_shutdown": True,
                                 "runtime_model": "meta-models/muse-spark-open-1026"}}

    def list_targets(self):
        return [self._target_view(row) for row in self.store.list_targets()]

    def _target_has_active_work(self, target_id: str) -> bool:
        if self._task and not self._task.done() and self._active_id:
            active = self.store.get_investigation(self._active_id)
            if active and active.get("target_id") == target_id:
                return True
        return self.store.target_has_active_work(target_id)

    def _publish(self, event: dict) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def enroll(self, spec):
        if self.store.get_target(spec.target_id) is not None:
            raise OpsError("target_already_enrolled", "This application ID is already enrolled.", 409)
        if self.store.is_retired(spec.target_id):
            raise OpsError("target_id_retired", "This application ID was retired and cannot be re-enrolled.", 409)
        # Enrollment is never implicit permission to act.
        if spec.policy.automatic_actions or spec.policy.auto_investigate:
            raise OpsError("enrollment_observe_only", "Enroll first, then explicitly configure its automation policy.")
        if spec.source_dir:
            from .source import SourceIndex
            index = await asyncio.to_thread(SourceIndex, spec)
            spec.source_manifest_sha256 = index.provenance_hash
        # Rechecked atomically after any SourceIndex await.
        row = self.store.enroll_target(spec.model_dump(mode="json"))
        await self.emit("target_enrolled", {"target": row}, spec.target_id)
        return self._target_view(row)

    async def update_target(self, target_id, expected_revision, spec):
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise OpsError("target_revision_changed", "Expected revision must be a nonnegative integer.", 409)
        current = self._target_record(target_id)
        if connection_revision(current) != expected_revision:
            raise OpsError("target_revision_changed", "Connection revision changed; reload the target and retry.", 409)
        if spec.target_id != target_id or spec.docker_context != current.get("docker_context") \
                or spec.container_name != current.get("container_name"):
            raise OpsError("target_identity_immutable",
                           "target_id, docker_context and container_name cannot be changed; enroll a new connection instead.", 409)
        if current.get("monitoring") is True:
            raise OpsError("target_must_be_disconnected", "Disconnect this connection before editing it.", 409)
        if spec.monitoring:
            raise OpsError("target_must_be_disconnected", "Editing must not enable monitoring; reconnect explicitly afterwards.", 409)
        if spec.policy.automatic_actions or spec.policy.auto_investigate:
            raise OpsError("enrollment_observe_only", "Editing cannot grant automation; use the policy endpoint after reconnecting.")
        self._mutation_blocked(target_id)
        expected_current = json.loads(json.dumps(current))
        new_spec = spec.model_copy(deep=True)
        if new_spec.source_dir:
            from .source import SourceIndex
            index = await asyncio.to_thread(SourceIndex, new_spec)
            new_spec.source_manifest_sha256 = index.provenance_hash
        else:
            new_spec.source_manifest_sha256 = None
        previous_cooldown = (current.get("policy") or {}).get("restart_cooldown_seconds", 600)
        if isinstance(previous_cooldown, bool) or not isinstance(previous_cooldown, int):
            previous_cooldown = 600
        new_spec.monitoring = False
        new_spec.policy = Policy(automatic_actions=[], auto_investigate=False,
                                 restart_cooldown_seconds=previous_cooldown)
        # Recheck live work after the awaited validation, then CAS in store.
        async with self._lock:
            self._mutation_blocked(target_id)
            record, effective, event = self.store.update_target_registration(
                target_id, expected_revision, expected_current, new_spec.model_dump(mode="json"))
        if effective:
            self.observations.pop(target_id, None)
            self.histories.pop(target_id, None)
            self.bad_streak.pop(target_id, None)
            if event is not None:
                self._publish(event)
        return self._target_view(record)

    async def remove_target(self, target_id, expected_revision):
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise OpsError("target_revision_changed", "Expected revision must be a nonnegative integer.", 409)
        current = self._target_record(target_id)
        if connection_revision(current) != expected_revision:
            raise OpsError("target_revision_changed", "Connection revision changed; reload the target and retry.", 409)
        if current.get("monitoring") is True:
            raise OpsError("target_must_be_disconnected", "Disconnect this connection before removing it.", 409)
        self._mutation_blocked(target_id)
        expected_current = json.loads(json.dumps(current))
        async with self._lock:
            self._mutation_blocked(target_id)
            result, event = self.store.remove_target_registration(
                target_id, expected_revision, expected_current)
        self.observations.pop(target_id, None)
        self.histories.pop(target_id, None)
        self.bad_streak.pop(target_id, None)
        if event is not None:
            self._publish(event)
        return result

    async def set_policy(self, target_id, policy):
        # No await between load and CAS commit, so a concurrent profile
        # edit/removal either wins first (CAS fails) or is overwritten by
        # nobody: the store compares the complete current record.
        current = self._target_record(target_id)
        spec = self.target(target_id)
        spec.policy = policy
        row = self.store.cas_replace_target(current, spec.model_dump(mode="json"))
        await self.emit("policy_changed", {"policy": policy.model_dump(mode="json")}, target_id)
        return self._target_view(row)

    async def disconnect(self, target_id):
        self.target(target_id)
        for row in self.store.scan_investigations(target_id):
            if row["phase"] in ACTIVE:
                await self.cancel(row["investigation_id"])
        # Deliberately not serialized by _lock: lifecycle entry points such
        # as prepare_patch hold it across an awaited observation and call
        # disconnect from there. The synchronous CAS below still prevents
        # resurrecting a removed record or overwriting a newer profile.
        current = self._target_record(target_id)
        spec = self.target(target_id)
        spec.monitoring = False
        self.store.cas_replace_target(current, spec.model_dump(mode="json"))
        await self.emit("target_disconnected", {"container_stopped": False}, target_id)
        return {"target_id": target_id, "monitoring": False, "container_stopped": False}

    async def reconnect(self, target_id):
        current = self._target_record(target_id)
        spec = self.target(target_id)
        spec.monitoring = True
        self.bad_streak[target_id] = 0
        row = self.store.cas_replace_target(current, spec.model_dump(mode="json"))
        await self.emit("target_reconnected", {}, target_id)
        return {"target_id": target_id, "monitoring": True}

    async def _observe(self, spec, *, _expected_record=None):
        try:
            observation = await self.docker_factory(spec).collect()
        except OpsError as exc:
            observation = {"timestamp": utc_now(), "healthy": None, "error": {"code": exc.code, "message": exc.message}}
        observation = redact_data(observation)
        if _expected_record is not None:
            # Monitor collection raced a profile edit/removal: discard stale.
            if self.store.get_target(spec.target_id) != _expected_record:
                return observation
        self.observations[spec.target_id] = observation
        rows = self.histories.setdefault(spec.target_id, deque(maxlen=120))
        rows.append({k: v for k, v in observation.items() if k not in ("logs", "config")})
        return observation

    async def _monitor(self):
        while not self._closed:
            try:
                try:
                    self.docker_status = await runtime_info()
                except OpsError as exc:
                    self.docker_status = {"available": False, "code": exc.code, "message": exc.message}
                for row in self.store.list_targets():
                    record_before = self.store.get_target(row["target_id"])
                    if record_before is None:
                        continue
                    try:
                        spec = self.target(row["target_id"])
                    except OpsError:
                        continue
                    if not spec.monitoring:
                        continue
                    observation = await self._observe(spec, _expected_record=record_before)
                    if self.store.get_target(row["target_id"]) != record_before:
                        continue
                    try:
                        spec = self.target(spec.target_id)
                    except OpsError:
                        continue
                    if not spec.monitoring:
                        continue
                    resource = observation.get("resources", {})
                    used, limit = resource.get("memory_bytes"), resource.get("memory_limit_bytes")
                    pressure = used is not None and limit and used / limit >= .90
                    degraded = observation.get("healthy") is False or bool(pressure)
                    self.bad_streak[spec.target_id] = self.bad_streak.get(spec.target_id, 0) + 1 if degraded else 0
                    await self.emit("observation", observation, spec.target_id)
                    if self.bad_streak[spec.target_id] == 3:
                        if self.store.list_unresolved_unknowns(spec.target_id):
                            await self.emit("attention_needed", {
                                "reason": "Previous operation outcome unknown; reconcile target before automatic investigation.",
                                "code": "target_reconciliation_required"}, spec.target_id)
                        else:
                            await self.emit("attention_needed", {"reason": "Sustained degradation detected."}, spec.target_id)
                            if spec.policy.auto_investigate and self.provider_ready:
                                await self.start_investigation(spec.target_id, "Investigate sustained measured degradation.", trigger="monitor")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Monitoring failure is explicit; no action is inferred from missing data.
                await self.emit("monitor_error", {"message": "Observation could not complete. No automatic action was taken."})
            await asyncio.sleep(self.interval)

    async def capture_snapshot(self, target_id):
        async with self._lock:
            # Same-target pending approval or in-flight work blocks baseline
            # replacement, including records outside UI pagination. Other idle
            # targets are not blocked by unrelated history.
            if self._capture_blocked(target_id):
                raise OpsError("operation_busy", "Finish or decide the pending operation for this target before replacing its baseline.", 409)
            spec = self.target(target_id)
            enrolled = spec.model_dump(mode="json")
            observation = await self._observe(spec)
            # Preflight without persisting so the final guard runs before any
            # replacement bytes are stored.
            result = await self.executor.capture_snapshot(spec, observation, persist=False)
            # Final synchronous guard: no await between these checks and the
            # store below, so in-process tasks cannot interleave. The service
            # lock serializes entry points; direct external SQLite writers and
            # container changes during the dispatch CLI itself remain possible
            # and are documented in docs/LIFECYCLE_RECOVERY.md.
            fresh = self.target(target_id)
            if fresh.model_dump(mode="json") != enrolled:
                raise OpsError("policy_changed", "Target enrollment changed during capture; baseline was not replaced.", 409)
            if self._capture_blocked(target_id):
                raise OpsError("operation_busy", "Target state changed during capture; baseline was not replaced. Review pending work and retry.", 409)
            self.store.put_setting(f"snapshot.{target_id}", result)
            await self.emit("baseline_saved", {"snapshot": result}, target_id)
            return result

    async def _collect_review_pair(self, spec, cancel):
        async def pair():
            first = await self._observe(spec)
            check_complete_observation(spec, first)
            if cancel.is_set():
                raise asyncio.CancelledError()
            second = await self._observe(spec)
            check_complete_observation(spec, second)
            return first, second
        try:
            return await asyncio.wait_for(pair(), timeout=self.reconciliation_timeout)
        except asyncio.TimeoutError as exc:
            raise OpsError("reconciliation_collection_timeout",
                           "Fresh target evidence could not be collected in time.", 409) from exc

    async def collect_reconciliation(self, target_id, cancel=None):
        """Read-only review of the current target state and its unknown set.

        Never calls a model, never unlocks the gate and never retries an old
        action. A report alone, a healthy response or elapsed time proves
        nothing about the earlier command.
        """
        cancel = cancel or asyncio.Event()
        spec = self.target(target_id)
        target_record = self.store.get_target(target_id)
        unknowns = self.store.list_unresolved_unknowns(target_id)
        if not unknowns:
            raise OpsError("reconciliation_not_required",
                           "This target has no unresolved unknown operation.", 409)
        if self._target_has_active_work(target_id):
            raise OpsError("reconciliation_active_operation",
                           "Finish or cancel the active operation before reviewing.", 409)
        if cancel.is_set():
            raise asyncio.CancelledError()
        first, second = await self._collect_review_pair(spec, cancel)
        if cancel.is_set():
            raise asyncio.CancelledError()
        if self.store.get_target(target_id) != target_record or {
                item["proposal_id"]: proposal_content_hash(item) for item in unknowns} != {
                item["proposal_id"]: proposal_content_hash(item)
                for item in self.store.list_unresolved_unknowns(target_id)}:
            raise OpsError("reconciliation_state_changed",
                           "Target or unknown operations changed while collecting; collect a fresh review.", 409)
        spec = self.target(target_id)
        target_record = self.store.get_target(target_id)
        unknowns = self.store.list_unresolved_unknowns(target_id)
        if not unknowns:
            raise OpsError("reconciliation_not_required",
                           "This target has no unresolved unknown operation.", 409)
        if self._target_has_active_work(target_id):
            raise OpsError("reconciliation_active_operation",
                           "Finish or cancel the active operation before reviewing.", 409)
        import uuid
        review = build_review(review_id=uuid.uuid4().hex, target_record=target_record,
                              spec=spec, unknowns=unknowns, first=first, second=second)
        saved = self.store.save_review(review)
        await self.emit("reconciliation_review_created",
                        {"review_id": saved["review_id"], "expires_at": saved["expires_at"],
                         "unresolved_unknown_ids": [item.get("proposal_id") for item in unknowns]},
                        target_id)
        return saved

    def get_reconciliation(self, target_id, review_id):
        self.target(target_id)
        review = self.store.get_review(review_id)
        if review is None or review.get("target_id") != target_id:
            raise OpsError("review_not_found", "Reconciliation review was not found.", 404)
        return review

    def _remaining_gate(self, target_id):
        unknowns = self.store.list_unresolved_unknowns(target_id)
        return {"reconciliation_required": bool(unknowns),
                "unresolved_unknown_ids": [item.get("proposal_id") for item in unknowns]}

    async def confirm_reconciliation(self, target_id, review_id, *, review_hash,
                                     attestation, operator_note, cancel=None):
        """Consume an exact reviewed record after another fresh check.

        Reports `outcome_reconciled`, never verified/mitigated/fixed, and never
        updates an investigation to success. A consumed confirmation replays its
        saved result idempotently with the live remaining gate state.
        """
        cancel = cancel or asyncio.Event()
        check_attestation(attestation)
        note = check_operator_note(operator_note)
        self.target(target_id)
        review = self.store.get_review(review_id)
        if review is None or review.get("target_id") != target_id:
            raise OpsError("review_not_found", "Reconciliation review was not found.", 404)
        if review.get("status") == "consumed":
            if not hashes_match(str(review.get("review_hash")), review_hash):
                raise OpsError("review_hash_mismatch",
                               "Confirmation does not match the reviewed record.", 409)
            confirmation = review.get("confirmation") or {}
            return {"result": "outcome_reconciled", "review_id": review_id, "target_id": target_id,
                    "idempotent_replay": True, "confirmed_at": confirmation.get("confirmed_at"),
                    "reconciled_ids": confirmation.get("reconciled_ids", []),
                    "expired_proposals": confirmation.get("expired_proposals", []),
                    "event_seq": confirmation.get("event_seq"),
                    "remaining_gate": self._remaining_gate(target_id)}
        if review.get("status") != "pending":
            raise OpsError("review_expired", "The review expired; collect a fresh review.", 409)
        if not hashes_match(str(review.get("review_hash")), review_hash):
            raise OpsError("review_hash_mismatch",
                           "Confirmation does not match the reviewed record.", 409)
        if not isinstance(review.get("expires_at"), str) or review["expires_at"] <= utc_now():
            try:
                self.store.save_review({**review, "status": "expired"})
            except OpsError:
                pass
            raise OpsError("review_expired", "The review expired; collect a fresh review.", 409)
        spec = self.target(target_id)
        target_record = self.store.get_target(target_id)
        if self._target_has_active_work(target_id):
            raise OpsError("reconciliation_active_operation",
                           "Finish or cancel the active operation before confirming.", 409)
        if cancel.is_set():
            raise asyncio.CancelledError()
        expected = {str(entry.get("proposal_id")): entry.get("content_hash")
                    for entry in review.get("unknowns") or []}
        live = {item["proposal_id"]: proposal_content_hash(item)
                for item in self.store.list_unresolved_unknowns(target_id)}
        if live != expected:
            raise OpsError("reconciliation_state_changed",
                           "Unknown operation records changed; collect a fresh review.", 409)
        first, second = await self._collect_review_pair(spec, cancel)
        if cancel.is_set():
            raise asyncio.CancelledError()
        # No await between the final local checks below and the commit call.
        spec = self.target(target_id)
        target_record = self.store.get_target(target_id)
        if self._target_has_active_work(target_id):
            raise OpsError("reconciliation_active_operation",
                           "Finish or cancel the active operation before confirming.", 409)
        from .actions import _hash as _action_hash, _spec_hash as _action_spec_hash
        if target_record.get("created_at") != review.get("target_created_at") \
                or _action_spec_hash(spec) != review.get("spec_hash") \
                or _action_hash(spec.policy.model_dump(mode="json")) != review.get("policy_hash"):
            raise OpsError("reconciliation_state_changed",
                           "Target enrollment changed; collect a fresh review.", 409)
        live = {item["proposal_id"]: proposal_content_hash(item)
                for item in self.store.list_unresolved_unknowns(target_id)}
        if live != expected:
            raise OpsError("reconciliation_state_changed",
                           "Unknown operation records changed; collect a fresh review.", 409)
        fresh = require_stable_pair(first, second)
        reviewed = {"identity": review.get("identity"), "runtime": review.get("runtime"),
                    "config": review.get("config"), "probes": review.get("probes"),
                    "healthy": review.get("healthy")}
        if fresh["identity"] != reviewed["identity"] or fresh["runtime"] != reviewed["runtime"]:
            raise OpsError("target_changed", "Target state changed since the review.", 409)
        if fresh["config"] != reviewed["config"]:
            raise OpsError("config_changed", "Target configuration changed since the review.", 409)
        if fresh["probes"] != reviewed["probes"] or fresh["healthy"] != reviewed["healthy"]:
            raise OpsError("reconciliation_probes_changed",
                           "Probe results changed since the review.", 409)
        require_recognized_image(self.store.list_unresolved_unknowns(target_id), fresh["identity"])
        committed = self.store.confirm_reconciliation_review(
            review_id=review_id, review_hash=review_hash, target_id=target_id,
            expected_target=target_record, expected_unknowns=expected, operator_note=note)
        self._publish(committed["event"])
        return {"result": "outcome_reconciled", "review_id": review_id, "target_id": target_id,
                "idempotent_replay": False, "confirmed_at": committed["review"]["confirmation"]["confirmed_at"],
                "reconciled_ids": committed["reconciled_ids"],
                "expired_proposals": committed["expired_proposals"],
                "event_seq": committed["event"]["seq"],
                "remaining_gate": self._remaining_gate(target_id)}

    async def start_investigation(self, target_id, question, trigger="manual"):
        async with self._lock:
            spec = self.target(target_id)
            if not spec.monitoring:
                raise OpsError("target_disconnected", "Reconnect this application before investigating.", 409)
            if trigger == "monitor" and not spec.policy.auto_investigate:
                raise OpsError("policy_changed", "Automatic investigation was disabled.", 409)
            if trigger == "monitor" and self.store.list_unresolved_unknowns(target_id):
                raise OpsError("target_reconciliation_required",
                               "A previous operation outcome is unknown; reconcile the target before automatic investigation.", 409)
            if not self.provider_ready:
                raise OpsError("provider_unavailable", "Configure the local Baseten key before starting an investigation.", 503)
            for row in self.store.scan_investigations(target_id):
                if row["phase"] in ACTIVE:
                    return row
            record = self.store.get_target(target_id)
            row = self.store.create_investigation(target_id, question, trigger=trigger,
                                                  connection_revision=connection_revision(record))
            await self.emit("investigation_queued", row, target_id, row["investigation_id"])
            self._kick()
            return row

    def _kick(self):
        if self._closed or self._task and not self._task.done():
            return
        rows = self.store.scan_investigations()
        if any(row["phase"] == "awaiting_approval" for row in rows):
            return
        queued = [row for row in rows if row["phase"] == "queued"]
        if not queued:
            self._active_id = None
            return
        row = queued[-1]
        self._active_id = row["investigation_id"]
        self._cancel = asyncio.Event()
        self._task = asyncio.create_task(self._investigate(row, self._cancel))
        self._task.add_done_callback(lambda _: self._kick())

    async def phase(self, row, phase, **changes):
        # An error describes its phase, not every later attempt. Prior errors
        # remain in durable events; a successful retry must not display one.
        changes.setdefault("error", None)
        result = self.store.update_investigation(row["investigation_id"], phase=phase, **changes)
        await self.emit("investigation_changed", result, row["target_id"], row["investigation_id"])
        return result

    async def _investigate(self, row, cancel):
        spec = self.target(row["target_id"])
        try:
            self._check_generation(row)
            if not spec.monitoring or row.get("trigger") == "monitor" and not spec.policy.auto_investigate:
                raise OpsError("policy_changed", "Investigation permission changed while queued.", 409)
            if row.get("trigger") == "monitor":
                from .actions import _check_reconciliation
                _check_reconciliation(self.store, spec.target_id)
            await self.phase(row, "investigating")
            observation = await self._observe(spec)
            fresh_spec = self.target(spec.target_id)
            self._check_generation(row)
            if not fresh_spec.monitoring or row.get("trigger") == "monitor" and not fresh_spec.policy.auto_investigate:
                raise OpsError("policy_changed", "Investigation permission changed during collection.", 409)
            if row.get("trigger") == "monitor":
                _check_reconciliation(self.store, spec.target_id)
            spec = fresh_spec
            if observation.get("error"):
                raise OpsError(observation["error"]["code"], observation["error"]["message"], 503)
            diagnosis_identity = dict(observation.get("identity", {}))
            evidence = {"observation.current": observation,
                        "observation.history": list(self.histories.get(spec.target_id, []))[-20:],
                        "operator.question": {"text": row["question"]},
                        "target.policy": spec.policy.model_dump(mode="json"),
                        "target.baseline": self.store.get_setting("snapshot." + spec.target_id),
                        "history.confirmed": [{"investigation_id": past["investigation_id"], "phase": past["phase"],
                            "diagnosis": past.get("diagnosis"), "updated_at": past["updated_at"],
                            "identity": past.get("evidence", {}).get("observation.current", {}).get("identity"),
                            "source_revision": past.get("source_revision"), "verification": past.get("verification")}
                            for past in self.store.list_investigations(spec.target_id, limit=8)
                            if past["phase"] in ("mitigated", "verified_fixed")][:3]}
            async def event(kind, data):
                if not cancel.is_set():
                    if kind == "provider_usage":
                        current = self.investigation(row["investigation_id"])
                        self.store.update_investigation(row["investigation_id"],
                            calls=[*(current.get("calls") or []), data])
                    await self.emit(kind, data, spec.target_id, row["investigation_id"])
            outcome = await asyncio.wait_for(self.investigator.diagnose(spec, evidence, cancel, event,
                refresh=lambda: self._observe(spec)), timeout=360)
            if cancel.is_set():
                raise asyncio.CancelledError()
            self.store.update_investigation(row["investigation_id"], evidence=outcome["evidence"], diagnosis=outcome["diagnosis"], calls=outcome.get("calls", []), source_revision=spec.source_revision)
            diagnosis = outcome["diagnosis"]
            if diagnosis["action"] == "abstain":
                await self.phase(row, "needs_human", summary=diagnosis["summary"])
                return
            # Recollect before proposing. The proposal binds the current observed identity.
            observation = await self._observe(spec)
            if observation.get("error"):
                raise OpsError("observation_unavailable", "Cannot authorize an action without fresh observations.", 409)
            if observation.get("identity") != diagnosis_identity:
                raise OpsError("target_changed", "The application changed during diagnosis. Start a new investigation.", 409)
            proposal = self.executor.create_proposal(spec, row["investigation_id"], diagnosis, observation)
            await self.phase(row, "awaiting_approval", proposal_id=proposal["proposal_id"])
            await self.emit("proposal_created", proposal, spec.target_id, row["investigation_id"])
            if proposal.get("blocked_reason"):
                self.store.transition_proposal(proposal["proposal_id"], "proposed", "blocked")
                if proposal["blocked_reason"] == "target_reconciliation_required":
                    message = "A previous operation outcome is unknown; reconcile the target before further action."
                elif proposal["blocked_reason"] == "restart_cooldown":
                    minutes = max(600, spec.policy.restart_cooldown_seconds) / 60
                    message = (f"This app requires at least {minutes:g} minutes between recovery actions. "
                               "After that period ends, start a new check for a fresh proposal. "
                               "This earlier proposal will stay blocked.")
                else:
                    message = "This action needs a verified baseline or additional provenance before it can be executed."
                await self.phase(row, "needs_human", error={"code": proposal["blocked_reason"], "message": message})
            elif proposal.get("automation_eligible") and ActionKind(diagnosis["action"]) in self.target(spec.target_id).policy.automatic_actions:
                await self._act(row, proposal, cancel, automatic=True)
        except asyncio.CancelledError:
            await self._record_cancel(row)
        except (OpsError, ProviderCallError, ProviderOutputError, asyncio.TimeoutError) as exc:
            code = getattr(exc, "code", None) or getattr(exc, "category", None) or "investigation_timeout"
            message = exc.message if isinstance(exc, OpsError) else "Investigation could not be completed. No success has been inferred."
            await self.phase(row, "needs_human", error={"code": code, "message": message})
        except Exception:
            await self.phase(row, "needs_human", error={"code": "investigation_error", "message": "Investigation failed. Review evidence before retrying."})

    async def decide(self, proposal_id, proposal_hash, approve):
        async with self._lock:
            proposal = self.store.get_proposal(proposal_id)
            if not proposal:
                raise OpsError("proposal_not_found", "Proposal was not found.", 404)
            if proposal.get("proposal_hash") != proposal_hash:
                raise OpsError("proposal_changed", "The proposal changed. Review the current proposal.", 409)
            if proposal.get("status") != "proposed":
                raise OpsError("proposal_not_pending", "This proposal has already been decided.", 409)
            row = self.investigation(proposal["investigation_id"])
            if row["phase"] != "awaiting_approval":
                raise OpsError("investigation_not_waiting", "The investigation is no longer awaiting approval.", 409)
            self._check_generation(row)
            if self._task and not self._task.done():
                raise OpsError("operation_busy", "Wait for the active operation to finish.", 409)
            if not approve:
                self.store.transition_proposal(proposal_id, "proposed", "rejected")
                await self.phase(row, "needs_human", summary="Proposed action was declined.")
                self._kick()
                return self.store.get_proposal(proposal_id)
            if self.store.list_unresolved_unknowns(row["target_id"]):
                raise OpsError("target_reconciliation_required",
                               "A previous operation outcome is unknown; reconcile the target before approving.", 409)
            self._active_id = row["investigation_id"]
            self._cancel = asyncio.Event()
            self._task = asyncio.create_task(self._act(row, proposal, self._cancel))
            self._task.add_done_callback(lambda _: self._kick())
            return {"proposal_id": proposal_id, "status": "accepted"}

    async def _act(self, row, proposal, cancel, automatic=False):
        claimed = False
        try:
            spec = self.target(row["target_id"])
            self._check_generation(row)
            if not spec.monitoring or cancel.is_set():
                raise asyncio.CancelledError()
            if self.store.list_unresolved_unknowns(row["target_id"]):
                raise OpsError("target_reconciliation_required",
                               "A previous operation outcome is unknown; reconcile the target before acting.", 409)
            if automatic and ActionKind(proposal["action"]) not in spec.policy.automatic_actions:
                raise OpsError("policy_changed", "Automatic action is no longer permitted.", 409)
            if self.store.transition_proposal(proposal["proposal_id"], "proposed", "executing", automatic=automatic,
                    authorization="policy" if automatic else "user", approved_hash=proposal["proposal_hash"]) is None:
                raise OpsError("proposal_not_pending", "This action was already claimed.", 409)
            claimed = True
            await self.phase(row, "executing")
            if proposal["action"] == ActionKind.DEPLOY.value:
                if automatic:
                    raise OpsError("deployment_approval_required", "Deployments require explicit approval.", 409)
                await self._deploy(row, proposal, spec, cancel)
                return
            result = await self.executor.execute(proposal, spec, cancel)
            self.store.update_proposal(proposal["proposal_id"], status="executed", result=result)
            await self.phase(row, "verifying")
            verification = await self.verify(spec, cancel, expected_identity=result.get("identity"))
            self.store.update_proposal(proposal["proposal_id"], verification=verification)
            await self.phase(row, "mitigated" if verification["verified"] else "needs_human", verification=verification)
        except asyncio.CancelledError:
            stored = self.store.get_proposal(proposal["proposal_id"])
            if stored and stored.get("status") == "verified":
                current = self.investigation(row["investigation_id"])
                if current.get("phase") == "verified_fixed":
                    if not current.get("baseline_error"):
                        error = {"code": "promotion_interrupted",
                                 "message": "Verified fix preserved; baseline promotion was interrupted. Retry promotion after review."}
                        self.store.update_investigation(row["investigation_id"], baseline_error=error)
                        await self.emit("baseline_promotion_failed", error, row["target_id"], row["investigation_id"])
                    return
            if _is_definitely_never_dispatched(stored, row["target_id"]):
                try:
                    self.store.update_proposal(proposal["proposal_id"], status="cancelled",
                                               error="cancelled_before_dispatch")
                except OpsError:
                    pass
                await self.phase(row, "cancelled")
            else:
                try:
                    self.store.update_proposal(proposal["proposal_id"], status="unknown",
                                               error="Cancelled while executing or verifying; check actual target state.")
                except OpsError:
                    pass
                await self.phase(row, "needs_human", error={"code": "action_outcome_unknown", "message": "Action interrupted. Inspect the application before retrying."})
        except Exception as exc:
            code = getattr(exc, "code", "action_failed")
            stored = self.store.get_proposal(proposal["proposal_id"])
            # A rejected retry does not own an existing execution or its outcome.
            # In particular, never convert unknown to failed and lift its gate.
            if stored and (claimed or stored.get("status") == "proposed"):
                if stored.get("status") == "verified":
                    return
                uncertain = (code == "action_outcome_unknown"
                    or stored.get("status") == "unknown"
                    or claimed and not _is_definitely_never_dispatched(stored, row["target_id"]))
                if uncertain:
                    code = "action_outcome_unknown"
                self.store.update_proposal(proposal["proposal_id"],
                    status="unknown" if uncertain else "failed", error=code)
            await self.phase(row, "needs_human", error={"code": code, "message": getattr(exc, "message", "Action failed. No recovery has been claimed.")})

    async def verify(self, spec, cancel, expected_identity=None):
        start, since, expected = time.monotonic(), None, expected_identity
        samples = []
        incomplete_samples = 0
        blocked_source = None
        while time.monotonic() - start < self.verification_timeout:
            if cancel.is_set():
                raise asyncio.CancelledError()
            observation = await self._observe(spec)
            samples.append({k: v for k, v in observation.items() if k not in ("logs", "config")})
            identity = observation.get("identity")
            if expected is not None and identity is not None and identity != expected:
                return {"verified": False, "kind": "availability", "root_cause_fixed": False,
                        "reason": "Application identity changed during recovery verification.",
                        "reason_code": "identity_changed", "sample_count": len(samples),
                        "incomplete_samples": incomplete_samples, "samples": samples[-30:]}
            try:
                timestamp = datetime.fromisoformat(observation.get("timestamp", "").replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - timestamp).total_seconds()
            except (ValueError, TypeError):
                age = 999
            collection_state, collection_source = check_collection(observation)
            resources_state, resources_source = check_resources(observation.get("resources"))
            runtime_state, runtime_source = check_runtime(observation)
            probes_state, probes_source = check_probes(spec, observation)
            missing = next((source for state, source in (
                (collection_state, collection_source), (resources_state, resources_source),
                (runtime_state, runtime_source), (probes_state, probes_source))
                if state in ("failed", "incomplete")), None)
            identity_ok = isinstance(identity, dict) and all(
                identity.get(k) for k in ("container_id", "image_id", "started_at"))
            if missing is None and not identity_ok:
                missing = "identity"
            if missing is None and not 0 <= age <= 10:
                missing = "freshness"
            if missing is not None:
                # Incomplete evidence resets the healthy interval; the memory
                # check below never runs on unknown measurements.
                incomplete_samples += 1
                blocked_source = blocked_source or missing
                healthy = False
            else:
                resources = observation.get("resources") or {}
                healthy = (observation.get("healthy") is True and runtime_state == "ok"
                           and probes_state == "ok"
                           and (resources_state == "unlimited" or pressure_ok(resources)))
            if healthy and (expected is None or identity == expected):
                expected = identity
                if since is None:
                    since = time.monotonic()
                if time.monotonic() - since >= self.verification_seconds:
                    return {"verified": True, "kind": "availability", "root_cause_fixed": False,
                            "reason_code": "recovery_observed", "sample_count": len(samples),
                            "incomplete_samples": incomplete_samples,
                            "duration_seconds": time.monotonic() - since, "samples": samples[-30:]}
            else:
                since = None
            await asyncio.sleep(min(self.interval, 1.0))
        if since is None and incomplete_samples == len(samples) and samples:
            return {"verified": False, "kind": "availability", "root_cause_fixed": False,
                    "reason": "Stable application recovery was not observed: "
                              f"incomplete telemetry ({blocked_source}).",
                    "reason_code": "telemetry_incomplete", "sample_count": len(samples),
                    "incomplete_samples": incomplete_samples, "samples": samples[-30:]}
        return {"verified": False, "kind": "availability", "root_cause_fixed": False,
                "reason": "Stable application recovery was not observed.",
                "reason_code": "recovery_not_observed", "sample_count": len(samples),
                "incomplete_samples": incomplete_samples, "samples": samples[-30:]}

    async def _record_cancel(self, row):
        current = self.investigation(row["investigation_id"])
        if current["phase"] in ("executing", "verifying"):
            proposals = self.store.list_proposals(row["investigation_id"])
            if proposals and all(_is_definitely_never_dispatched(item, row["target_id"]) for item in proposals):
                await self.phase(row, "cancelled")
            else:
                # Persist the gate before cancelling the coroutine: its handler
                # may never run (for example cancellation before its first step).
                for item in proposals:
                    if item.get("status") in ("executing", "dispatching", "verifying", "rolling_back", "executed") \
                            and not _is_definitely_never_dispatched(item, row["target_id"]):
                        self.store.update_proposal(item["proposal_id"], status="unknown",
                            error="Cancellation left the operation outcome unresolved.")
                await self.phase(row, "needs_human", error={"code": "action_outcome_unknown", "message": "Inspect the application before retrying."})
        elif current["phase"] != "needs_human":
            await self.phase(row, "cancelled")

    async def cancel(self, investigation_id):
        row = self.investigation(investigation_id)
        if row["phase"] not in ACTIVE:
            return row
        if self._active_id == investigation_id and self._task and not self._task.done():
            task = self._task
            # Invalidate durable queue state BEFORE cancellation: the coroutine may
            # not have started yet, so its try/finally is not guaranteed to execute.
            await self._record_cancel(row)
            self._cancel.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await self.phase(row, "cancelled")
        for proposal in self.store.list_proposals(investigation_id):
            if proposal["status"] == "proposed":
                self.store.transition_proposal(proposal["proposal_id"], "proposed", "expired")
        self._kick()
        return self.investigation(investigation_id)

    async def prepare_patch(self, investigation_id):
        async with self._lock:
            row = self.investigation(investigation_id)
            spec = self.target(row["target_id"])
            self._check_generation(row)
            if not spec.monitoring:
                raise OpsError("target_disconnected", "Reconnect the application before preparing a fix.", 409)
            if not row.get("diagnosis"):
                raise OpsError("diagnosis_required", "Complete an investigation before preparing a fix.", 409)
            if not spec.source_dir or not spec.patch_profile:
                raise OpsError("patch_profile_required", "Enroll verified source and an isolated test profile before preparing a fix.", 409)
            # Explicit phase gate: never overwrite completed, tested or active
            # work. All rejections below happen before any writes, preserving
            # patch/proposal/verification/baseline_error. No model call decides
            # entry; only durable bindings and a fresh identity read do.
            phase = row.get("phase")
            if phase not in PREPARE_PATCH_ALLOWED_PHASES:
                if phase in ACTIVE:
                    raise OpsError("operation_busy", "Complete or cancel the active operation first.", 409)
                if phase == "fix_proposed":
                    raise OpsError("fix_already_proposed", "A tested fix already exists for this investigation. Use prepare-deployment to rebuild its proposal.", 409)
                if phase in ("verified_fixed", "mitigated"):
                    raise OpsError("investigation_completed", "This investigation is already resolved; start a new investigation for new work.", 409)
                raise OpsError("investigation_not_eligible", "This investigation cannot start patch preparation from its current phase.", 409)
            existing_patch = row.get("patch") or {}
            if isinstance(existing_patch, dict) \
                    and existing_patch.get("status") == "tests_passed":
                raise OpsError("fix_already_proposed", "A tested fix already exists for this investigation. Use prepare-deployment to rebuild its proposal.", 409)
            if (self._task and not self._task.done()) or any(
                    item.get("phase") in ACTIVE for item in self.store.scan_investigations()):
                raise OpsError("operation_busy", "Complete or cancel the active operation first.", 409)
            # Obsolete diagnosis/source bindings fail before paid work.
            if not row.get("source_revision") \
                    or row.get("source_revision") != spec.source_revision:
                raise OpsError("source_changed", "Enrolled source changed since diagnosis; start a new investigation.", 409)
            if isinstance(existing_patch, dict) and existing_patch:
                if ("investigation_id" in existing_patch
                        and existing_patch.get("investigation_id") != investigation_id) \
                        or ("target_id" in existing_patch
                            and existing_patch.get("target_id") != row["target_id"]):
                    raise OpsError("diagnosis_obsolete", "Existing patch does not belong to this investigation; start a new investigation.", 409)
                if "source_revision" in existing_patch \
                        and existing_patch.get("source_revision") != spec.source_revision:
                    raise OpsError("source_changed", "Enrolled source changed since the previous patch attempt; start a new investigation.", 409)
            evidence = row.get("evidence") or {}
            diagnosis_identity = None
            if isinstance(evidence, dict):
                current_obs = evidence.get("observation.current") or {}
                if isinstance(current_obs, dict):
                    diagnosis_identity = current_obs.get("identity")
            if not isinstance(diagnosis_identity, dict) \
                    or not diagnosis_identity.get("container_id"):
                raise OpsError("diagnosis_obsolete", "Diagnosis lacks a bound container identity; start a new investigation.", 409)
            observation = await self._observe(spec)
            if observation.get("error") \
                    or not isinstance(observation.get("identity"), dict):
                raise OpsError("observation_unavailable", "Cannot verify target identity before preparing a fix.", 409)
            live_identity = observation.get("identity") or {}
            for key in ("container_id", "image_id", "started_at"):
                if live_identity.get(key) != diagnosis_identity.get(key):
                    raise OpsError("target_changed", "The application changed since diagnosis. Start a new investigation.", 409)
            if spec.source_image_id \
                    and live_identity.get("image_id") != spec.source_image_id:
                raise OpsError("target_changed", "The deployed image changed since diagnosis. Start a new investigation.", 409)
            # disconnect/cancel/policy changes need not acquire this entry lock.
            # Recheck after collection, before any paid job or phase write.
            fresh_spec = self.target(row["target_id"])
            self._check_generation(row)
            if fresh_spec.model_dump(mode="json") != spec.model_dump(mode="json") \
                    or self.investigation(investigation_id) != row:
                raise OpsError("preparation_state_changed", "Application or investigation changed while checking. Review its current state before preparing a fix.", 409)
            if (self._task and not self._task.done()) or any(
                    item.get("phase") in ACTIVE for item in self.store.scan_investigations()):
                raise OpsError("operation_busy", "Complete or cancel the active operation first.", 409)
            if self.patch_engine is None:
                from .patches import PatchEngine
                self.patch_engine = PatchEngine(self.store, self.investigator.adapter, self.root / ".local" / "ops")
            self._active_id = investigation_id
            self._cancel = asyncio.Event()
            self._task = asyncio.create_task(self._patch(row, spec, self._cancel))
            self._task.add_done_callback(lambda _: self._kick())
            return await self.phase(row, "preparing_patch")

    async def _patch(self, row, spec, cancel):
        try:
            self._check_generation(row)
            async def event(kind, data):
                if not cancel.is_set():
                    await self.emit(kind, data, spec.target_id, row["investigation_id"])
            result = await asyncio.wait_for(self.patch_engine.prepare(spec, row, cancel, event), timeout=900)
            if cancel.is_set():
                raise asyncio.CancelledError()
            await self.phase(row, "fix_proposed" if result.get("status") == "tests_passed" else "needs_human", patch=result)
        except asyncio.CancelledError:
            await self._record_cancel(row)
        except Exception as exc:
            await self.phase(row, "needs_human", error={"code": getattr(exc, "code", "patch_failed"), "message": getattr(exc, "message", "Patch preparation failed; no changes were deployed.")})

    async def prepare_deployment(self, investigation_id):
        async with self._lock:
            row = self.investigation(investigation_id)
            spec = self.target(row["target_id"])
            self._check_generation(row)
            retry = row["phase"] in ("needs_human", "cancelled")
            if not spec.monitoring or row["phase"] not in ("fix_proposed", "needs_human", "cancelled") or row.get("patch", {}).get("status") != "tests_passed":
                raise OpsError("tested_fix_required", "A connected target and tested fix are required.", 409)
            if retry:
                rebuildable, reason = self._startup_rebuild_check(row, allow_retry=True)
                if not rebuildable:
                    raise OpsError("deployment_retry_blocked", reason or "Review this retained fix before retrying deployment preparation.", 409)
            if not spec.recovery_checks:
                raise OpsError("recovery_checks_required", "Configure an independent complete-operation check before deployment.", 409)
            if (self._task and not self._task.done()) or any(
                    item.get("phase") in ACTIVE for item in self.store.scan_investigations()):
                raise OpsError("operation_busy", "Complete or cancel the active operation first.", 409)
            if retry:
                # Rebuilding never reuses approval from a failed preparation.
                for proposal in self.store.list_proposals(investigation_id):
                    if proposal.get("status") == "proposed":
                        self.store.update_proposal(proposal["proposal_id"], status="expired")
            self._active_id = investigation_id
            self._cancel = asyncio.Event()
            await self.phase(row, "preparing_deployment")
            self._task = asyncio.create_task(self._build_deployment(row, spec, self._cancel))
            self._task.add_done_callback(lambda _: self._kick())
            return self.investigation(investigation_id)

    async def promote_deployment(self, proposal_id):
        """Record a proven deployment as the current version, without redeploying."""
        async with self._lock:
            if (self._task and not self._task.done()) or any(
                    row["phase"] in ACTIVE for row in self.store.scan_investigations()):
                raise OpsError("operation_busy", "Finish the active investigation before promoting its baseline.", 409)
            proposal = self.store.get_proposal(proposal_id)
            if not proposal:
                raise OpsError("proposal_not_found", "Deployment was not found.", 404)
            check_target_generation(self.store, proposal["target_id"], proposal.get("investigation_id"))
            from .promotion import promote
            result = await promote(self.deployment_manager, self.target(proposal["target_id"]), proposal_id)
            self.store.update_investigation(proposal["investigation_id"], baseline_error=None,
                                            baseline_promoted=True)
            await self.emit("deployment_promoted", {"proposal_id": proposal_id,
                "source_manifest_sha256": result["source_manifest_sha256"],
                "identity": result["identity"], "container_mutated": False}, proposal["target_id"], proposal["investigation_id"])
            return result

    async def _build_deployment(self, row, spec, cancel):
        try:
            self._check_generation(row)
            observation = await self._observe(spec)
            self._check_generation(row)
            proposal = await asyncio.wait_for(self.deployment_manager.build_proposal(
                spec, row["investigation_id"], row["patch"], observation), timeout=240)
            if cancel.is_set():
                self.store.update_proposal(proposal["proposal_id"], status="expired")
                raise asyncio.CancelledError()
            await self.phase(row, "awaiting_approval", proposal_id=proposal["proposal_id"])
            await self.emit("proposal_created", proposal, spec.target_id, row["investigation_id"])
        except asyncio.CancelledError:
            await self._record_cancel(row)
        except Exception as exc:
            await self.phase(row, "needs_human", error={"code": getattr(exc, "code", "deployment_build_failed"),
                "message": getattr(exc, "message", "Could not build the verified deployment. The application was not changed.")})

    async def _deploy(self, row, proposal, spec, cancel):
        self._check_generation(row)
        result = await self.deployment_manager.execute(proposal, spec, cancel)
        self.store.update_proposal(proposal["proposal_id"], status="verifying", result=result)
        await self.phase(row, "verifying")
        availability = await self.verify(spec, cancel, expected_identity=result["identity"])
        functional = {"verified": False, "reason": "Availability was not restored."}
        if availability["verified"]:
            functional = await self.functional_verifier(spec, cancel, docker_factory=self.docker_factory)
            identity_fields = ("container_id", "image_id", "started_at")
            actual_identity = functional.get("identity") or {}
            if any(not actual_identity.get(key) or actual_identity.get(key) != result["identity"].get(key) for key in identity_fields):
                functional = {**functional, "verified": False, "reason": "Application identity changed during functional verification."}
        verified = availability["verified"] and functional.get("verified") is True
        verification = {"verified": verified, "kind": "tested_patch_and_runtime", "root_cause_fixed": verified,
                        "availability": availability, "functional": functional,
                        "duration_seconds": availability.get("duration_seconds"),
                        "reason": None if verified else functional.get("reason", "Runtime correction was not verified.")}
        self.store.update_proposal(proposal["proposal_id"], status="verified" if verified else "verification_failed", verification=verification)
        if verified:
            await self.phase(row, "verified_fixed", verification=verification)
            try:
                from .promotion import promote
                promoted = await promote(self.deployment_manager, spec, proposal["proposal_id"])
                self.store.update_investigation(row["investigation_id"], baseline_promoted=True, baseline_error=None)
                await self.emit("deployment_promoted", {"proposal_id": proposal["proposal_id"],
                    "identity": promoted["identity"], "container_mutated": False}, spec.target_id, row["investigation_id"])
            except asyncio.CancelledError:
                # Interrupted bookkeeping never undoes a proven runtime correction.
                error = {"code": "promotion_interrupted",
                         "message": "Verified fix preserved; baseline promotion was interrupted. Retry promotion after review."}
                self.store.update_investigation(row["investigation_id"], baseline_error=error)
                await self.emit("baseline_promotion_failed", error, spec.target_id, row["investigation_id"])
            except Exception as exc:
                # Bookkeeping failure does not undo a proven runtime correction.
                # Keep the old binding fail-closed and offer an explicit retry.
                error = {"code": getattr(exc, "code", "promotion_failed"),
                         "message": getattr(exc, "message", "The fix is verified, but its version baseline could not be saved. Retry after reviewing application state.")}
                self.store.update_investigation(row["investigation_id"], baseline_error=error)
                await self.emit("baseline_promotion_failed", error, spec.target_id, row["investigation_id"])
            return
        # Rollback is part of the reviewed proposal, and only the exact known
        # deployment identity is eligible. Unknown outcomes never retry blindly.
        if proposal.get("rollback_on_failure") is True and not cancel.is_set():
            try:
                rollback = await self.deployment_manager.rollback(proposal, spec, cancel)
                restored = await self.verify(spec, cancel, expected_identity=rollback["identity"])
                verification["rollback"] = {"result": rollback, "availability": restored}
                self.store.update_proposal(proposal["proposal_id"], status="rolled_back", verification=verification)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                verification["rollback"] = {"error": getattr(exc, "code", "rollback_failed")}
                self.store.update_proposal(proposal["proposal_id"], status="unknown", verification=verification)
        await self.phase(row, "needs_human", verification=verification)
