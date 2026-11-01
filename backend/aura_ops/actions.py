"""Typed, version-bound operations; command completion is never recovery proof."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from .docker import CommandResult, DockerTarget, run_command
from .models import ActionKind, OpsError, TargetSpec, utc_now
from .redaction import redact_data, redact_text
from .store import OpsStore
from .target_management import check_target_generation


MAX_CONFIG_BYTES = 1024 * 1024
_IDENTITY_KEYS = ("container_id", "image_id", "image_ref", "started_at")
_BOUND_FIELDS = ("version", "target_id", "investigation_id", "action",
                 "expected_identity", "policy_hash", "spec_hash", "snapshot_hash", "reason")


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise OpsError("invalid_action_data", "Action data is not valid JSON.") from exc


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identity(observation: dict) -> dict:
    raw = observation.get("identity")
    if not isinstance(raw, dict):
        raise OpsError("identity_missing", "Current container identity is required.", 409)
    result = {key: raw.get(key) for key in _IDENTITY_KEYS}
    if not re.fullmatch(r"[a-f0-9]{64}", str(result["container_id"])) \
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(result["image_id"])) \
            or not isinstance(result["image_ref"], str) or not result["image_ref"] \
            or not isinstance(result["started_at"], str) or not result["started_at"]:
        raise OpsError("identity_missing", "Complete container and image identity is required.", 409)
    return result


def _fresh(observation: dict) -> None:
    try:
        measured = datetime.fromisoformat(observation["timestamp"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - measured).total_seconds()
        if age < -5 or age > 60:
            raise ValueError("stale")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise OpsError("stale_observation", "A fresh observation is required before proposing an action.", 409) from exc


def _file_hash(path: Path, *, optional: bool = False) -> str | None:
    if optional and not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise OpsError("config_unavailable", "The enrolled configuration file is unavailable.", 409) from exc
    if len(data) > MAX_CONFIG_BYTES:
        raise OpsError("config_too_large", "Configuration exceeds its inspection limit.", 413)
    return hashlib.sha256(data).hexdigest()


def _spec_hash(spec: TargetSpec) -> str:
    return _hash(spec.model_dump(mode="json"))


def _proposal_hash(proposal: dict) -> str:
    if not all(key in proposal for key in _BOUND_FIELDS):
        raise OpsError("invalid_proposal", "Proposal is missing its execution binding.", 409)
    return _hash({key: proposal[key] for key in _BOUND_FIELDS})


def _check_reconciliation(store, target_id: str) -> None:
    """Fail-closed target gate: any unresolved unknown blocks new mutation."""
    if store.list_unresolved_unknowns(target_id):
        raise OpsError("target_reconciliation_required",
                       "A previous operation for this target ended with an unknown outcome. "
                       "Reconcile its actual state before further action.", 409)


class ActionExecutor:
    def __init__(self, store: OpsStore, docker_factory=DockerTarget, runner=None):
        self.store = store
        self.docker_factory = docker_factory
        self.runner = runner or run_command
        self.work_dir = self.store.root / "actions"
        self.work_dir.mkdir(exist_ok=True)
        if self.work_dir.is_symlink() or self.work_dir.resolve() != self.work_dir:
            raise OpsError("action_path_invalid", "Action workspace must stay inside the operations store.")
        self.empty_env = self.work_dir / "empty.env"
        if not self.empty_env.exists():
            self.empty_env.write_bytes(b"")

    def _compose_files(self, spec: TargetSpec) -> dict:
        if not spec.compose_file or not spec.compose_project or not spec.compose_service:
            raise OpsError("compose_not_enrolled", "This target has no enrolled Compose configuration.", 409)
        path = Path(spec.compose_file)
        if not path.is_absolute():
            raise OpsError("config_path_invalid", "The enrolled Compose path must be absolute.", 409)
        path = path.resolve()
        adjacent_env = path.parent / ".env"
        return {"compose_file": str(path), "compose_sha256": _file_hash(path),
                "env_file": str(adjacent_env), "env_sha256": _file_hash(adjacent_env, optional=True)}

    def _compose_argv(self, spec: TargetSpec, files: dict) -> list[str]:
        # No implicit CWD .env or global Docker context. Explicit CLI options
        # override ambient Compose project/file selection.
        if self.empty_env.is_symlink() or self.empty_env.read_bytes() != b"":
            raise OpsError("config_changed", "The internal empty environment file changed.", 409)
        env_file = files["env_file"] if files["env_sha256"] is not None else str(self.empty_env)
        return ["docker", "--context", spec.docker_context, "compose",
                "--project-directory", str(Path(files["compose_file"]).parent),
                "--env-file", env_file, "-p", spec.compose_project,
                "-f", files["compose_file"]]

    async def _resolved_config(self, spec: TargetSpec, files: dict) -> dict:
        result = await self.runner(self._compose_argv(spec, files) + ["config", "--format", "json"],
                                   timeout=15, max_stdout=MAX_CONFIG_BYTES, max_stderr=65536)
        if result.returncode:
            # Compose errors can echo interpolated secret values.
            raise OpsError("compose_invalid", "The enrolled Compose configuration could not be resolved.", 409)
        try:
            config = json.loads(result.stdout)
            service = config["services"][spec.compose_service]
        except (ValueError, TypeError, KeyError) as exc:
            raise OpsError("compose_invalid", "The enrolled service is absent from resolved Compose configuration.", 409) from exc
        if not isinstance(service, dict) or service.get("container_name") != spec.container_name:
            raise OpsError("compose_target_mismatch", "Compose must explicitly name the enrolled container.", 409)
        if (service.get("deploy") or {}).get("replicas", 1) != 1:
            raise OpsError("compose_replicas_unsupported", "Only a single enrolled container may be restored.", 409)
        if not isinstance(service.get("image"), str) or not service["image"]:
            raise OpsError("compose_image_missing", "The enrolled service must name an existing image.", 409)
        if service.get("post_start") or service.get("pre_stop"):
            raise OpsError("compose_lifecycle_unsupported",
                           "Compose lifecycle hooks cannot run during recovery; enroll a hook-free server profile.", 409)
        # `config` expands env_file/include/extends and interpolation. Files
        # referenced by configs/secrets need content hashes as well as paths.
        external_hashes = {}
        for kind in ("configs", "secrets"):
            entries = config.get(kind) or {}
            if not isinstance(entries, dict):
                raise OpsError("compose_invalid", "Unexpected Compose dependency structure.", 409)
            for key, definition in entries.items():
                if isinstance(definition, dict) and definition.get("file"):
                    dependency = Path(definition["file"])
                    if not dependency.is_absolute():
                        dependency = Path(files["compose_file"]).parent / dependency
                    external_hashes[f"{kind}:{key}"] = _file_hash(dependency.resolve())
                elif isinstance(definition, dict) and definition.get("external"):
                    raise OpsError("compose_external_unsupported", "External secrets/configs require explicit provenance before replay.", 409)
        return {"resolved_sha256": _hash({"config": config, "file_content_hashes": external_hashes}),
                "image_ref": service["image"]}

    async def _image_provenance(self, spec: TargetSpec, image_id: str) -> dict:
        result = await self.runner(["docker", "--context", spec.docker_context, "image", "inspect", image_id],
                                   timeout=15, max_stdout=MAX_CONFIG_BYTES, max_stderr=65536)
        if result.returncode:
            return {"verified": False, "reason": "image_metadata_unavailable"}
        try:
            rows = json.loads(result.stdout)
            image = rows[0] if isinstance(rows, list) and len(rows) == 1 else {}
            labels = (image.get("Config") or {}).get("Labels") or {}
            revision = labels.get("org.opencontainers.image.revision")
            migration_free = labels.get("aura.migration-free") == "true"
            verified = bool(spec.source_revision and image.get("Id") == image_id
                            and revision == spec.source_revision and migration_free)
        except (ValueError, TypeError, AttributeError):
            verified, revision, migration_free = False, None, False
        # Arbitrary labels, registry credentials and source URLs never persist.
        return {"verified": verified, "source_revision": revision if verified else None,
                "migration_free": bool(verified and migration_free)}

    async def capture_snapshot(self, spec: TargetSpec, observation: dict, *, persist=True) -> dict:
        _fresh(observation)
        identity = _identity(observation)
        if observation.get("healthy") is not True:
            raise OpsError("baseline_unhealthy", "A known-good snapshot requires a healthy observation.", 409)
        config = observation.get("config") or {}
        snapshot = {"snapshot_id": uuid.uuid4().hex, "target_id": spec.target_id,
                    "timestamp": utc_now(), "identity": identity,
                    "source_revision": spec.source_revision, "source_dir": spec.source_dir,
                    "configuration": redact_data({key: config.get(key) for key in
                                                  ("memory_limit_bytes", "published_ports", "server_port")}),
                    "compose": None, "provenance": {"verified": False}}
        if spec.compose_file:
            files = self._compose_files(spec)
            resolved = await self._resolved_config(spec, files)
            if resolved["image_ref"] not in (identity["image_ref"], identity["image_id"]):
                raise OpsError("compose_image_mismatch", "Compose does not describe the observed healthy image.", 409)
            # Re-read after the asynchronous inspection so a concurrent file
            # edit cannot become a healthy baseline unnoticed.
            if self._compose_files(spec) != files:
                raise OpsError("config_changed", "Configuration changed during snapshot capture.", 409)
            snapshot["compose"] = {**files, **resolved,
                                   "project": spec.compose_project, "service": spec.compose_service}
            snapshot["provenance"] = await self._image_provenance(spec, identity["image_id"])
        current = await self.docker_factory(spec, runner=self.runner).inspect_identity()
        if any(current.get(key) != identity.get(key) for key in _IDENTITY_KEYS):
            raise OpsError("target_changed", "The target changed during known-good snapshot capture.", 409)
        _fresh(observation)
        if persist:
            self.store.put_setting(f"snapshot.{spec.target_id}", snapshot)
        return snapshot

    def create_proposal(self, spec: TargetSpec, investigation_id: str,
                        diagnosis_dict: dict, observation: dict) -> dict:
        check_target_generation(self.store, spec.target_id, investigation_id)
        _fresh(observation)
        identity = _identity(observation)
        try:
            action = ActionKind(diagnosis_dict.get("action"))
        except (TypeError, ValueError) as exc:
            raise OpsError("action_not_allowed", "Unknown remediation action.", 409) from exc
        if action == ActionKind.ABSTAIN:
            raise OpsError("no_action", "Abstention does not create an execution proposal.", 409)
        snapshot = self.store.get_setting(f"snapshot.{spec.target_id}")
        reason = redact_text(str(diagnosis_dict.get("reason") or diagnosis_dict.get("summary") or ""))[:1500]
        if not reason.strip():
            raise OpsError("reason_required", "An evidence-based action reason is required.", 409)
        proposal = {"version": 1, "target_id": spec.target_id,
                    "investigation_id": investigation_id, "action": action.value,
                    "expected_identity": identity, "policy_hash": _hash(spec.policy.model_dump(mode="json")),
                    "spec_hash": _spec_hash(spec),
                    "snapshot_hash": _hash(snapshot) if snapshot and action in (ActionKind.RESTORE, ActionKind.ROLLBACK) else None,
                    "reason": reason}
        eligible = action in spec.policy.automatic_actions
        blocked = None
        if action in (ActionKind.RESTORE, ActionKind.ROLLBACK):
            if not snapshot or not snapshot.get("compose"):
                eligible, blocked = False, "known_good_snapshot_required"
            elif action == ActionKind.RESTORE and identity["image_id"] != snapshot["identity"]["image_id"]:
                eligible, blocked = False, "restore_cannot_change_image"
            elif action == ActionKind.ROLLBACK and not snapshot.get("provenance", {}).get("verified"):
                eligible, blocked = False, "rollback_provenance_required"
        if action == ActionKind.DEPLOY:
            eligible, blocked = False, "verified_patch_required"
        if action in (ActionKind.RESTORE, ActionKind.ROLLBACK) \
                and (spec.source_dir or spec.source_revision or spec.source_image_id) \
                and spec.deployment_launch is None:
            eligible, blocked = False, "deployment_launch_required"
        if action in (ActionKind.RESTART, ActionKind.RESTORE, ActionKind.ROLLBACK) \
                and self._disruption_cooldown_active(spec):
            eligible = False
            if blocked is None:
                blocked = "restart_cooldown"
        if self.store.list_unresolved_unknowns(spec.target_id):
            # Reconciliation overrides cooldown/provenance blocks: elapsed time
            # or a new baseline never proves the old command outcome.
            eligible, blocked = False, "target_reconciliation_required"
        proposal.update({"proposal_hash": _proposal_hash(proposal),
                         "automation_eligible": eligible, "blocked_reason": blocked})
        return self.store.put_proposal(proposal)

    async def _check_snapshot(self, spec: TargetSpec, proposal: dict) -> dict:
        snapshot = self.store.get_setting(f"snapshot.{spec.target_id}")
        if not snapshot or not snapshot.get("compose"):
            raise OpsError("snapshot_required", "A healthy enrolled configuration snapshot is required.", 409)
        if _hash(snapshot) != proposal["snapshot_hash"]:
            raise OpsError("snapshot_changed", "Known-good snapshot changed after this proposal.", 409)
        stored = snapshot["compose"]
        current = self._compose_files(spec)
        if any(current[key] != stored[key] for key in current) \
                or stored["project"] != spec.compose_project or stored["service"] != spec.compose_service:
            raise OpsError("config_changed", "Configuration or .env changed; this proposal cannot replay it.", 409)
        resolved = await self._resolved_config(spec, current)
        if resolved["resolved_sha256"] != stored["resolved_sha256"]:
            raise OpsError("config_changed", "Resolved configuration changed; a new reviewed snapshot is required.", 409)
        if self._compose_files(spec) != current:
            raise OpsError("config_changed", "Configuration changed while action preconditions were checked.", 409)
        fresh = self.store.get_setting(f"snapshot.{spec.target_id}")
        if not fresh or not fresh.get("compose"):
            raise OpsError("snapshot_required", "A healthy enrolled configuration snapshot is required.", 409)
        if _hash(fresh) != proposal["snapshot_hash"]:
            raise OpsError("snapshot_changed", "Known-good snapshot changed while action preconditions were checked.", 409)
        return snapshot

    async def execute(self, proposal: dict, spec: TargetSpec,
                      cancel_event: asyncio.Event | None = None) -> dict:
        cancel_event = cancel_event or asyncio.Event()
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        _check_reconciliation(self.store, spec.target_id)
        check_target_generation(self.store, spec.target_id, proposal.get("investigation_id"))
        stored = self.store.get_proposal(proposal.get("proposal_id", ""))
        if stored is None or stored.get("proposal_hash") != proposal.get("proposal_hash") \
                or _proposal_hash(proposal) != proposal.get("proposal_hash") \
                or _proposal_hash(stored) != stored.get("proposal_hash"):
            raise OpsError("proposal_changed", "The proposal does not match its persisted execution binding.", 409)
        if stored.get("status") != "executing":
            raise OpsError("action_not_claimed", "The service must approve and claim this proposal before execution.", 409)
        if proposal["target_id"] != spec.target_id or proposal["spec_hash"] != _spec_hash(spec) \
                or proposal["policy_hash"] != _hash(spec.policy.model_dump(mode="json")):
            raise OpsError("policy_changed", "Target enrollment or policy changed after the proposal.", 409)
        action = ActionKind(proposal["action"])
        authorization = stored.get("authorization")
        if authorization == "policy":
            if action not in spec.policy.automatic_actions:
                raise OpsError("action_not_authorized", "The current target policy does not permit this action.", 403)
        elif authorization == "user":
            if stored.get("approved_hash") != stored["proposal_hash"]:
                raise OpsError("action_not_authorized", "Approval does not match this proposal.", 403)
        else:
            raise OpsError("action_not_authorized", "An explicit policy or user authorization is required.", 403)
        if action == ActionKind.DEPLOY:
            raise OpsError("verified_patch_required", "Deployment requires a separately verified patch artifact.", 409)
        if action == ActionKind.ABSTAIN:
            raise OpsError("action_not_allowed", "Abstention cannot be executed.", 409)
        if action in (ActionKind.RESTORE, ActionKind.ROLLBACK) \
                and (spec.source_dir or spec.source_revision or spec.source_image_id) \
                and spec.deployment_launch is None:
            raise OpsError("deployment_launch_required", "Source-bound Compose replay requires an enrolled direct server launch without automatic migrations.", 409)
        expected = proposal["expected_identity"]
        dispatched = False

        async def tracked_runner(argv, **bounds):
            nonlocal dispatched
            mutation = (argv[3:5] == ["container", "restart"] or
                        (len(argv) > 3 and argv[3] == "compose" and "up" in argv))
            if mutation:
                if cancel_event.is_set():
                    raise asyncio.CancelledError()
                if dispatched:
                    raise OpsError("repeat_dispatch", "An action may dispatch only once.", 409)
                if authorization == "policy":
                    # A valid JSON diagnosis is not authority to disrupt a
                    # currently healthy app. Refresh measured conditions after
                    # all prior preflights and before consuming automatic scope.
                    observed = await self.docker_factory(spec, runner=self.runner).collect()
                    _fresh(observed)
                    if observed.get("error") or observed.get("collection_errors") \
                            or observed.get("healthy") not in (True, False):
                        raise OpsError("observation_unavailable", "Automatic action requires complete fresh observations.", 409)
                    if _identity(observed) != expected:
                        raise OpsError("target_changed", "The application changed during automatic preflight.", 409)
                    resources = observed.get("resources") or {}
                    used, limit = resources.get("memory_bytes"), resources.get("memory_limit_bytes")
                    memory_pressure = (isinstance(used, (int, float)) and not isinstance(used, bool)
                                       and isinstance(limit, (int, float)) and not isinstance(limit, bool)
                                       and limit > 0 and used >= .90 * limit)
                    if observed["healthy"] is True and not memory_pressure:
                        raise OpsError("automatic_action_unnecessary", "The application currently responds without measured memory pressure; automatic intervention was withheld.", 409)
                    if cancel_event.is_set():
                        raise asyncio.CancelledError()
                if action in (ActionKind.RESTORE, ActionKind.ROLLBACK):
                    # Final approved-config binding after all prior waits. Host
                    # files/containers can still change externally during the
                    # dispatch CLI itself; that residual race is documented and
                    # not claimed as an atomic transaction.
                    await self._check_snapshot(spec, proposal)
                # Inspection/config resolution above await external I/O. Policy,
                # enrollment, approval and files may change while pending.
                # Re-read durable authority at the dispatch boundary; there must
                # be no await between this check and dispatch intent.
                if cancel_event.is_set():
                    raise asyncio.CancelledError()
                enrolled = self.store.get_target(spec.target_id)
                if enrolled is None:
                    raise OpsError("target_not_found", "The application is no longer enrolled.", 409)
                current = TargetSpec.model_validate({key: value for key, value in enrolled.items()
                                                     if key in TargetSpec.model_fields})
                if not current.monitoring:
                    raise OpsError("target_disconnected", "The application was disconnected before dispatch.", 409)
                if _spec_hash(current) != proposal["spec_hash"] \
                        or _hash(current.policy.model_dump(mode="json")) != proposal["policy_hash"]:
                    raise OpsError("policy_changed", "Enrollment or policy changed during action preflight.", 409)
                fresh_proposal = self.store.get_proposal(stored["proposal_id"])
                if fresh_proposal is None or fresh_proposal.get("proposal_hash") != proposal.get("proposal_hash") \
                        or _proposal_hash(fresh_proposal) != fresh_proposal.get("proposal_hash") \
                        or _proposal_hash(proposal) != proposal.get("proposal_hash"):
                    raise OpsError("proposal_changed", "The proposal changed during action preflight.", 409)
                if fresh_proposal.get("status") != "executing":
                    raise OpsError("action_already_claimed", "This action was already dispatched or cancelled.", 409)
                fresh_auth = fresh_proposal.get("authorization")
                if fresh_auth != authorization:
                    raise OpsError("action_not_authorized", "Proposal authorization changed during action preflight.", 403)
                if fresh_auth == "policy":
                    if action not in current.policy.automatic_actions:
                        raise OpsError("action_not_authorized", "The current policy no longer permits this action.", 403)
                elif fresh_auth == "user":
                    if fresh_proposal.get("approved_hash") != fresh_proposal["proposal_hash"]:
                        raise OpsError("action_not_authorized", "Approval does not match this proposal.", 403)
                else:
                    raise OpsError("action_not_authorized", "An explicit policy or user authorization is required.", 403)
                if action == ActionKind.RESTART:
                    self._check_restart_cooldown(current)
                else:
                    self._check_disruption_cooldown(current)
                _check_reconciliation(self.store, spec.target_id)
                check_target_generation(self.store, spec.target_id, proposal.get("investigation_id"))
                # Persist intent BEFORE transmission. A crash or transport loss
                # leaves an unknown action to reconcile, never an automatic retry.
                claim = self.store.transition_proposal(stored["proposal_id"], "executing", "dispatching",
                                                       dispatch_attempted_at=utc_now())
                if claim is None:
                    raise OpsError("action_already_claimed", "This action was already dispatched or cancelled.", 409)
                self.store.put_setting(f"lastrestart.{spec.target_id}", time.time())
                dispatched = True
            return await self.runner(argv, **bounds)

        target = self.docker_factory(spec, runner=tracked_runner)
        actual = await target.inspect_identity()
        if any(actual.get(key) != expected.get(key) for key in _IDENTITY_KEYS):
            raise OpsError("target_changed", "Container identity changed after the proposal.", 409)
        snapshot = None
        if action == ActionKind.RESTART:
            self._check_restart_cooldown(spec)
        else:
            self._check_disruption_cooldown(spec)
            snapshot = await self._check_snapshot(spec, proposal)
            if action == ActionKind.RESTORE and actual["image_id"] != snapshot["identity"]["image_id"]:
                raise OpsError("restore_cannot_change_image", "Configuration restoration cannot silently change application code.", 409)
            if action == ActionKind.ROLLBACK:
                provenance = snapshot.get("provenance") or {}
                current_provenance = await self._image_provenance(spec, actual["image_id"])
                if not spec.source_revision or snapshot.get("source_revision") != spec.source_revision \
                        or snapshot.get("source_dir") != spec.source_dir \
                        or not provenance.get("verified") or not current_provenance.get("verified"):
                    raise OpsError("rollback_provenance_required", "Both images require the same enrolled source revision and explicit migration-free provenance.", 409)
        if cancel_event.is_set():
            raise asyncio.CancelledError()

        override = None
        async def perform():
            nonlocal override
            if action == ActionKind.RESTART:
                return await target.restart(expected, cancel_event)
            # A JSON Compose override cannot contain YAML tags or expressions.
            # It pins the known image ID for both configuration restore/rollback.
            override = self.work_dir / f"override-{uuid.uuid4().hex}.json"
            service_override = {"image": snapshot["identity"]["image_id"]}
            if spec.source_dir or spec.source_revision or spec.source_image_id:
                # Do not replay an image's historical bootstrap entrypoint:
                # it may migrate persistent data before the app starts.
                service_override.update(spec.deployment_launch.model_dump(mode="json"))
            override.write_bytes(_canonical({"services": {spec.compose_service: service_override}}))
            argv = self._compose_argv(spec, snapshot["compose"])
            argv += ["-f", str(override), "up", "-d", "--no-deps", "--no-build", "--pull", "never", spec.compose_service]
            # Re-inspect immediately before dispatch after resolving files.
            identity = await target.inspect_identity()
            if any(identity.get(key) != expected.get(key) for key in _IDENTITY_KEYS):
                raise OpsError("target_changed", "Container identity changed before configuration replay.", 409)
            result = await tracked_runner(argv, timeout=60, max_stdout=MAX_CONFIG_BYTES, max_stderr=65536)
            if result.returncode:
                raise OpsError("action_outcome_unknown", "Compose did not report success; reconcile the target before another action.", 409)
            after = await target.inspect_identity()
            if after["image_id"] != snapshot["identity"]["image_id"]:
                raise OpsError("action_outcome_unknown", "The resulting image does not match the approved immutable image.", 409)
            return {"action": action.value, "command_completed": True, "verified": False,
                    "identity": after, "timestamp": utc_now()}

        operation = asyncio.create_task(perform())
        cancellation = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({operation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
            if cancellation in done and cancel_event.is_set():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                if dispatched:
                    raise OpsError("action_outcome_unknown", "Action was cancelled after dispatch; reconcile the target before retrying.", 409)
                raise asyncio.CancelledError()
            result = await operation
            if not dispatched:
                raise OpsError("action_not_dispatched", "Adapter returned without executing the registered action.", 409)
            return {**result, "proposal_id": stored["proposal_id"], "verified": False}
        except asyncio.CancelledError as exc:
            if dispatched:
                raise OpsError("action_outcome_unknown", "Action interrupted after dispatch; reconcile the target before retrying.", 409) from exc
            raise
        except OpsError as exc:
            if dispatched and exc.code != "action_outcome_unknown":
                raise OpsError("action_outcome_unknown", "Action was dispatched but its final state is uncertain.", 409) from exc
            raise
        except Exception as exc:
            if dispatched:
                raise OpsError("action_outcome_unknown", "Action was dispatched but its final state is uncertain.", 409) from exc
            raise OpsError("action_failed", "Action failed before dispatch.", 409) from exc
        finally:
            for task in (operation, cancellation):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, cancellation, return_exceptions=True)
            if override is not None:
                override.unlink(missing_ok=True)

    def _disruption_cooldown_active(self, spec: TargetSpec) -> bool:
        last = self.store.get_setting(f"lastrestart.{spec.target_id}")
        if last is None:
            return False
        try:
            elapsed = time.time() - float(last)
        except (TypeError, ValueError):
            return True
        return elapsed < max(600, spec.policy.restart_cooldown_seconds)

    def _check_restart_cooldown(self, spec: TargetSpec) -> None:
        if self._disruption_cooldown_active(spec):
            raise OpsError("restart_cooldown", "A restart was recently attempted; investigate before retrying.", 409)

    def _check_disruption_cooldown(self, spec: TargetSpec) -> None:
        if self._disruption_cooldown_active(spec):
            raise OpsError("restart_cooldown",
                           "A disruptive recovery was recently attempted; investigate before retrying.", 409)
