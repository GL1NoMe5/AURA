"""Run lifecycle manager: one active run, events, reset, telemetry.

Owns the WorkerController for the app lifespan. Only worker verification
can resolve a run; reset invalidates the current run before awaiting
anything, so late orchestration/verification results are dropped.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from aura_worker import WorkerController
from aura_worker.controller import WorkerDeadError
from aura_worker.cache import CEILING_BYTES, HEALTHY_CACHE_BYTES
from aura_worker.controller import ControllerState

from .config import ApiSettings
from .orchestration import (
    EvidenceBundle,
    Orchestrator,
    OrchestratorUnavailableError,
    UnavailableOrchestrator,
    outcome_completeness,
    validate_outcome,
)
from .schemas import (
    ALL_AGENT_IDS,
    AgentSummary,
    ErrorBody,
    EventEnvelope,
    Phase,
    StateSnapshot,
    TelemetrySnapshot,
    VerificationSummary,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class RunContext:
    run_id: str
    scenario: str
    phase: Phase = Phase.FAULT_INJECTED
    created_at: str = field(default_factory=_utc_now)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    worker_generation: Optional[int] = None
    failed_jobs_at_start: int = 0
    agents: dict = field(default_factory=dict)
    proposal: Optional[dict] = None
    verification: Optional[VerificationSummary] = None
    error: Optional[ErrorBody] = None
    task: Optional[asyncio.Task] = None


class _RunEventSink:
    """Agent event sink that drops events from invalidated runs."""

    def __init__(self, manager: "IncidentManager", run: RunContext):
        self._manager = manager
        self._run = run

    async def agent_started(self, agent_id: str) -> None:
        if not self._manager._is_current(self._run) or agent_id not in ALL_AGENT_IDS:
            return
        self._manager._set_agent(self._run, agent_id, status="running")
        await self._manager._emit(
            "agent_started", {"agent_id": agent_id}, run_id=self._run.run_id)

    async def agent_message(self, agent_id: str, message: str,
                            kind: str = "status") -> None:
        if not self._manager._is_current(self._run) or agent_id not in ALL_AGENT_IDS:
            return
        await self._manager._emit(
            "agent_message",
            {"agent_id": agent_id, "kind": kind, "message": str(message)[:2000]},
            run_id=self._run.run_id)

    async def agent_finished(self, agent_id: str, status: str,
                             summary: Optional[str] = None) -> None:
        if not self._manager._is_current(self._run) or agent_id not in ALL_AGENT_IDS:
            return
        mapped = status if status in ("completed", "failed") else "failed"
        self._manager._set_agent(
            self._run, agent_id, status=mapped, summary=summary)
        await self._manager._emit(
            "agent_finished",
            {"agent_id": agent_id, "status": mapped, "summary": summary},
            run_id=self._run.run_id)


class IncidentManager:
    def __init__(
        self,
        controller=None,
        orchestrator: Optional[Orchestrator] = None,
        settings: Optional[ApiSettings] = None,
        provider_configured: bool = False,
    ) -> None:
        self.controller = controller if controller is not None \
            else WorkerController()
        self.orchestrator: Orchestrator = orchestrator \
            if orchestrator is not None else UnavailableOrchestrator()
        self.settings = settings or ApiSettings()
        self.provider_configured = provider_configured
        self.server_instance_id = uuid.uuid4().hex
        self._seq = 0
        self.journal: deque[dict] = deque(maxlen=self.settings.journal_maxlen)
        self._subscribers: set[asyncio.Queue] = set()
        self.dropped_for_slow_subscribers = 0
        self._current_run: Optional[RunContext] = None
        self._idle_phase = Phase.IDLE
        self._idle_error: Optional[ErrorBody] = None
        self._op_lock = asyncio.Lock()
        self._telemetry_task: Optional[asyncio.Task] = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------
    async def startup(self) -> None:
        if self._started:
            return
        await self.controller.start()
        self._started = True
        self._telemetry_task = asyncio.create_task(self._telemetry_loop())

    async def shutdown(self) -> None:
        async with self._op_lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        run = self._current_run
        self._current_run = None  # invalidate before awaiting anything
        if run is not None:
            run.cancel_event.set()
            if run.task is not None and not run.task.done():
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)
        if self._telemetry_task is not None:
            self._telemetry_task.cancel()
            await asyncio.gather(self._telemetry_task, return_exceptions=True)
            self._telemetry_task = None
        for queue in list(self._subscribers):
            self._subscribers.discard(queue)
        try:
            if self._started:
                await self.controller.stop()
                self._started = False
        finally:
            # Own the provider adapter lifetime: close it even when the
            # worker stop above fails.
            close = getattr(self.orchestrator, "aclose", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    # -- helpers -------------------------------------------------------------
    @property
    def orchestrator_ready(self) -> bool:
        return bool(getattr(self.orchestrator, "ready", False))

    def _is_current(self, run: RunContext) -> bool:
        return self._current_run is run and not run.cancel_event.is_set()

    def _set_agent(self, run: RunContext, agent_id: str, **updates) -> None:
        summary = run.agents.get(agent_id) or AgentSummary(agent_id=agent_id)
        for key, value in updates.items():
            if value is not None or key == "summary":
                setattr(summary, key, value)
        run.agents[agent_id] = summary

    def _new_run_agents(self) -> dict:
        return {agent_id: AgentSummary(agent_id=agent_id)
                for agent_id in ALL_AGENT_IDS}

    async def _emit(self, event_type: str, payload: dict,
                    run_id: Optional[str]) -> dict:
        self._seq += 1
        envelope = EventEnvelope(
            type=event_type, run_id=run_id, seq=self._seq,
            timestamp=_utc_now(), payload=payload,
            server_instance_id=self.server_instance_id,
        ).model_dump(mode="json")
        self.journal.append(envelope)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # Slow consumers never block sampling: drop their oldest
                # queued event; the seq gap documents the loss.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self.dropped_for_slow_subscribers += 1
                try:
                    queue.put_nowait(envelope)
                except asyncio.QueueFull:
                    pass
        return envelope

    def create_snapshot_event(self) -> dict:
        """Authoritative per-connection snapshot; not fanned out again."""
        snapshot = self.snapshot()
        self._seq += 1
        envelope = EventEnvelope(
            type="snapshot", run_id=snapshot.run_id, seq=self._seq,
            timestamp=_utc_now(), payload=snapshot.model_dump(mode="json"),
            server_instance_id=self.server_instance_id,
        ).model_dump(mode="json")
        self.journal.append(envelope)
        return envelope

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=self.settings.subscriber_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def _set_phase(self, run: RunContext, phase: Phase) -> None:
        previous = run.phase
        run.phase = phase
        await self._emit(
            "phase_changed", {"from": previous.value, "to": phase.value},
            run_id=run.run_id)

    # -- snapshots -----------------------------------------------------------
    def _telemetry(self) -> TelemetrySnapshot:
        try:
            snap = self.controller.snapshot()
        except Exception:
            snap = {}
        status = snap.get("worker_status") or {}
        worker_state = str(snap.get("state", "unknown"))
        fresh_status = snap.get("worker_status_stale") is False
        health = "unknown"
        if worker_state == ControllerState.DEAD.value:
            health = "dead"
        elif not snap.get("child_running"):
            health = "stopped"
        elif fresh_status and snap.get("rss_fresh"):
            health = "healthy" if (
                status.get("eviction_enabled") is True
                and status.get("retained_cache_bytes") == HEALTHY_CACHE_BYTES
                and worker_state in (
                    ControllerState.HEALTHY.value, ControllerState.RECOVERED.value)
            ) else "degraded"
        elif snap.get("child_running"):
            health = "degraded"
        return TelemetrySnapshot(
            rss_bytes=snap.get("rss_bytes"),
            baseline_rss_bytes=snap.get("baseline_rss_bytes"),
            retained_cache_bytes=(
                status.get("retained_cache_bytes") if fresh_status else None),
            completed_jobs=(
                status.get("completed_jobs") if fresh_status else None),
            failed_jobs=status.get("failed_jobs") if fresh_status else None,
            sample_timestamp=snap.get("rss_sample_timestamp"),
            status_timestamp=snap.get("worker_status_timestamp"),
            generation=snap.get("generation"),
            rss_fresh=bool(snap.get("rss_fresh")),
            status_fresh=fresh_status,
            worker_state=worker_state,
            worker_health=health,
        )

    def snapshot(self) -> StateSnapshot:
        run = self._current_run
        telemetry = self._telemetry()
        if run is not None:
            return StateSnapshot(
                seq=self._seq,
                run_id=run.run_id, phase=run.phase, scenario=run.scenario,
                server_instance_id=self.server_instance_id,
                worker_health=telemetry.worker_health, telemetry=telemetry,
                agents=[run.agents[agent_id] for agent_id in ALL_AGENT_IDS
                        if agent_id in run.agents],
                latest_verification=run.verification, proposal=run.proposal,
                error=run.error,
                simulated_diagnosis=bool(
                    getattr(self.orchestrator, "simulated", False)),
            )
        return StateSnapshot(
            seq=self._seq,
            run_id=None, phase=self._idle_phase, scenario=None,
            server_instance_id=self.server_instance_id,
            worker_health=telemetry.worker_health, telemetry=telemetry,
            agents=[AgentSummary(agent_id=agent_id) for agent_id in ALL_AGENT_IDS],
            latest_verification=None, proposal=None, error=self._idle_error,
            simulated_diagnosis=False,
        )

    async def _telemetry_loop(self) -> None:
        try:
            while True:
                run = self._current_run
                await self._emit(
                    "telemetry", self._telemetry().model_dump(mode="json"),
                    run_id=run.run_id if run else None)
                await asyncio.sleep(self.settings.telemetry_interval_seconds)
        except asyncio.CancelledError:
            raise

    # -- run lifecycle --------------------------------------------------------
    async def start_incident(self, scenario: str) -> RunContext:
        async with self._op_lock:
            if not self.orchestrator_ready:
                raise ApiError(
                    503, "orchestrator_unavailable",
                    "Diagnosis orchestrator is not available yet; "
                    "no fault was injected.")
            run = self._current_run
            if run is not None and run.phase not in (
                    Phase.RESOLVED, Phase.ERROR, Phase.CANCELLED):
                raise ApiError(
                    409, "run_already_active",
                    "An incident run is already active.")
            if run is not None and run.phase in (
                    Phase.ERROR, Phase.CANCELLED) \
                    and not self._worker_ready_for_new_run():
                # A failed run may leave a dirty/stopped worker and stale
                # fault state; require Reset before any new injection.
                raise ApiError(
                    409, "reset_required",
                    "The previous run failed; reset is required before "
                    "starting a new incident.")
            new_run = RunContext(
                run_id=uuid.uuid4().hex, scenario=scenario,
                agents=self._new_run_agents())
            self._current_run = new_run
            self._idle_error = None
            new_run.task = asyncio.create_task(self._run_lifecycle(new_run))
            return new_run

    def _worker_ready_for_new_run(self) -> bool:
        """Fresh healthy worker: eviction on, healthy cache, running."""
        try:
            snap = self.controller.snapshot()
        except Exception:
            return False
        status = snap.get("worker_status") or {}
        telemetry = self._telemetry()
        return bool(
            snap.get("child_running")
            and telemetry.rss_fresh and telemetry.status_fresh
            and status.get("eviction_enabled") is True
            and status.get("retained_cache_bytes") == HEALTHY_CACHE_BYTES
            and snap.get("state") in (
                ControllerState.HEALTHY.value,
                ControllerState.RECOVERED.value)
        )

    async def _wait_for_failure_evidence(self, run: RunContext) -> bool:
        """Await the measured ceiling plus at least one rejected job.

        Uses fresh status, the worker-reported ceiling when available and
        the run's generation; finite timeout; cancellation-aware through
        the run token checked on every poll.
        """
        deadline = time.monotonic() \
            + self.settings.failure_evidence_timeout_seconds
        while time.monotonic() < deadline:
            if not self._is_current(run):
                return False
            if run.worker_generation is not None and \
                    self.controller.generation != run.worker_generation:
                return False
            snap = self.controller.snapshot()
            status = snap.get("worker_status") or {}
            telemetry = self._telemetry()
            ceiling = status.get("ceiling_bytes") or CEILING_BYTES
            if telemetry.status_fresh and telemetry.rss_fresh \
                    and status.get("retained_cache_bytes") is not None \
                    and status["retained_cache_bytes"] >= ceiling \
                    and (telemetry.failed_jobs or 0) > run.failed_jobs_at_start:
                return True
            if "ceiling_bytes" not in status:
                # Controller doubles without a reported ceiling still must
                # show measured rejected jobs above the healthy bound.
                if telemetry.status_fresh \
                            and (telemetry.failed_jobs or 0) > run.failed_jobs_at_start \
                        and (telemetry.retained_cache_bytes or 0) \
                        > HEALTHY_CACHE_BYTES:
                    return True
            if snap.get("state") == ControllerState.DEAD.value:
                return False
            await asyncio.sleep(0.25)
        return False

    async def _fail_run(self, run: RunContext, code: str,
                        message: str) -> None:
        if not self._is_current(run):
            return
        run.error = ErrorBody(code=code, message=message)
        for agent in run.agents.values():
            if agent.status == "running":
                agent.status = "failed"
                agent.summary = "Stopped because the incident diagnosis failed."
        await self._set_phase(run, Phase.ERROR)
        await self._emit("error", {"code": code, "message": message},
                         run_id=run.run_id)

    async def _run_lifecycle(self, run: RunContext) -> None:
        try:
            await self._emit(
                "phase_changed", {"from": Phase.IDLE.value,
                                  "to": Phase.FAULT_INJECTED.value},
                run_id=run.run_id)
            # Counters survive a resolved run; require a NEW rejected job.
            run.failed_jobs_at_start = self._telemetry().failed_jobs or 0
            await self.controller.inject_cache_retention()
            if not self._is_current(run):
                return
            run.worker_generation = self.controller.generation
            detected = await self.controller.wait_for_detection(
                timeout=self.settings.detection_timeout_seconds)
            if not self._is_current(run):
                return
            if not detected:
                state = self.controller.snapshot().get("state")
                if state == ControllerState.DEAD.value:
                    await self._fail_run(
                        run, "worker_dead", "Worker process is not running.")
                else:
                    await self._fail_run(
                        run, "detection_timeout",
                        "Fault was not detected within the finite timeout.")
                return
            # Demonstrate the actual failure before any diagnosis: the
            # retained cache must reach its ceiling with rejected jobs.
            failure_ready = await self._wait_for_failure_evidence(run)
            if not self._is_current(run):
                return
            if not failure_ready:
                state = self.controller.snapshot().get("state")
                if state == ControllerState.DEAD.value:
                    await self._fail_run(
                        run, "worker_dead", "Worker process is not running.")
                else:
                    await self._fail_run(
                        run, "failure_evidence_timeout",
                        "Retained-cache ceiling and rejected jobs were "
                        "not observed within the finite timeout.")
                return
            await self._diagnose_and_remediate(run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Preserve a useful safe provider error category (auth_error,
            # rate_limit, timeout, invalid output, ...) without ever
            # exposing the raw exception, key or headers.
            code = getattr(exc, "code", None) or getattr(
                exc, "category", None) or "run_failed"
            await self._fail_run(
                run, str(code)[:64],
                "Incident run failed; see server state for details.")

    def _build_evidence(self, run: RunContext) -> EvidenceBundle:
        snap = self.controller.snapshot()
        telemetry = self._telemetry()
        baseline = snap.get("baseline_rss_bytes")
        measurements = snap.get("measurements") or []
        status = snap.get("worker_status") or {}
        worker_config = {
            key: status.get(key) for key in (
                "initialized", "eviction_enabled", "block_count",
                "block_size_bytes", "retained_cache_bytes",
                "healthy_cache_bytes", "ceiling_bytes")
            if key in status
        }
        # Actual worker diagnostic lines when the controller exposes them;
        # these are process log lines, not per-request records.
        raw_lines = getattr(self.controller, "stderr_lines", None)
        diagnostic_logs = [
            {"source": "worker_stderr", "text": str(line)[:500]}
            for line in list(raw_lines)[-20:]
        ] if raw_lines else []
        evidence_ids = ["telemetry.current", "telemetry.recent"]
        if status:
            evidence_ids += ["worker.status", "worker.config"]
        if diagnostic_logs:
            evidence_ids.append("diagnostic.logs")
        evidence_ids.append("chart.measured")
        # Retain up to 120 samples so the measured chart includes the
        # healthy baseline period before the fault, not only the fault.
        sampled = list(measurements[-120:])
        return EvidenceBundle(
            run_id=run.run_id, scenario=run.scenario,
            worker_generation=snap.get("generation"),
            measured_at=telemetry.sample_timestamp,
            baseline_rss_bytes=baseline,
            rss_bytes=telemetry.rss_bytes, rss_fresh=telemetry.rss_fresh,
            retained_cache_bytes=telemetry.retained_cache_bytes,
            healthy_cache_bytes=HEALTHY_CACHE_BYTES,
            ceiling_bytes=CEILING_BYTES,
            rss_threshold_bytes=(
                baseline + 24 * 1024 * 1024 if baseline is not None else None),
            completed_jobs=telemetry.completed_jobs,
            failed_jobs=telemetry.failed_jobs,
            worker_state=telemetry.worker_state,
            recent_measurements=sampled,
            worker_config=worker_config,
            worker_config_source=(
                "worker_status" if worker_config else "unavailable"),
            eviction_enabled=status.get("eviction_enabled"),
            status_fresh=telemetry.status_fresh,
            status_timestamp=telemetry.status_timestamp,
            sampled_telemetry=sampled,
            diagnostic_logs=diagnostic_logs,
            diagnostic_logs_source=(
                "worker_stderr" if diagnostic_logs else "unavailable"),
            evidence_ids=evidence_ids,
        )

    async def _diagnose_with_liveness(self, run, evidence, sink):
        async def watch_worker():
            while self._is_current(run):
                if (not self.controller.is_running
                        or self.controller.state in (ControllerState.DEAD, ControllerState.ERROR)
                        or self.controller.generation != run.worker_generation):
                    raise WorkerDeadError()
                await asyncio.sleep(0.1)
            raise asyncio.CancelledError()

        diagnosis = asyncio.create_task(self.orchestrator.diagnose(
            evidence, run.cancel_event, sink))
        watcher = asyncio.create_task(watch_worker())
        try:
            done, _ = await asyncio.wait(
                {diagnosis, watcher}, timeout=self.settings.orchestration_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise asyncio.TimeoutError()
            if watcher in done:
                await watcher
            return await diagnosis
        finally:
            for task in (diagnosis, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(diagnosis, watcher, return_exceptions=True)

    async def _diagnose_and_remediate(self, run: RunContext) -> None:
        await self._set_phase(run, Phase.DIAGNOSING)
        evidence = self._build_evidence(run)
        sink = _RunEventSink(self, run)
        try:
            raw = await self._diagnose_with_liveness(run, evidence, sink)
        except WorkerDeadError:
            await self._fail_run(run, "worker_dead", "Worker process stopped during diagnosis.")
            return
        except asyncio.TimeoutError:
            await self._fail_run(
                run, "diagnosis_timeout",
                "Diagnosis did not complete within the finite timeout.")
            return
        except OrchestratorUnavailableError:
            await self._fail_run(
                run, "orchestrator_unavailable",
                "Diagnosis orchestrator is not available.")
            return
        # A late result from an invalidated run is dropped here.
        if not self._is_current(run):
            return
        try:
            outcome = validate_outcome(raw)
        except Exception:
            await self._fail_run(
                run, "invalid_orchestration_output",
                "Orchestrator returned an invalid structured result.")
            return
        for finding in outcome.findings:
            self._set_agent(
                run, finding.agent_id,
                status=("completed" if finding.status == "completed"
                        else "failed"),
                summary=finding.summary, findings=list(finding.findings),
                evidence_refs=list(finding.evidence_refs),
                reasoning=finding.reasoning,
                reasoning_truncated=finding.reasoning_truncated,
                usage=finding.usage,
                requested_model=finding.requested_model,
                response_model=finding.response_model,
                duration_ms=finding.duration_ms, attempts=finding.attempts)
        proposal = outcome.proposal
        run.proposal = proposal.model_dump(mode="json")
        self._set_agent(
            run, "synthesis", status="completed", summary=proposal.reason,
            evidence_refs=list(proposal.evidence_refs),
            reasoning=proposal.reasoning,
            reasoning_truncated=proposal.reasoning_truncated,
            usage=proposal.usage,
            requested_model=proposal.requested_model,
            response_model=proposal.response_model,
            duration_ms=proposal.duration_ms, attempts=proposal.attempts)
        await self._emit(
            "action_proposed",
            {"action": proposal.action, "reason": proposal.reason,
             "evidence_refs": list(proposal.evidence_refs)},
            run_id=run.run_id)
        if not self._is_current(run):
            return
        incomplete = outcome_completeness(outcome)
        if incomplete is not None:
            await self._fail_run(
                run, incomplete,
                "Specialist findings are incomplete; no action was taken.")
            return
        if proposal.action == "abstain":
            await self._fail_run(
                run, "synthesis_abstained",
                "Synthesis abstained; no action was taken.")
            return
        if proposal.action != "restore_cache_policy":
            await self._fail_run(
                run, "action_not_allowed",
                "Proposed action is not in the allowlist.")
            return
        if not self._fault_preconditions_met(run):
            await self._fail_run(
                run, "fault_preconditions_unmet",
                "Measured fault preconditions are not met; "
                "no action was taken.")
            return
        await self._set_phase(run, Phase.REMEDIATING)
        await self._verify_with_worker(run)

    def _fault_preconditions_met(self, run: RunContext) -> bool:
        if run.worker_generation is not None and \
                self.controller.generation != run.worker_generation:
            return False
        snap = self.controller.snapshot()
        status = snap.get("worker_status")
        telemetry = self._telemetry()
        if not telemetry.rss_fresh or not telemetry.status_fresh \
                or not status:
            return False
        baseline = snap.get("baseline_rss_bytes")
        return bool(
            status.get("eviction_enabled") is False
            and (telemetry.retained_cache_bytes or 0) > HEALTHY_CACHE_BYTES
            and baseline is not None
            and telemetry.rss_bytes is not None
            and telemetry.rss_bytes > baseline + 24 * 1024 * 1024
        )

    async def _verify_with_worker(self, run: RunContext) -> None:
        applied = {"done": False}

        async def observe_verifying() -> None:
            while self._is_current(run) and not applied["done"]:
                if self.controller.state == ControllerState.VERIFYING:
                    applied["done"] = True
                    await self._emit(
                        "action_applied",
                        {"action": "restore_cache_policy",
                         "acknowledged": True},
                        run_id=run.run_id)
                    await self._set_phase(run, Phase.VERIFYING)
                    return
                await asyncio.sleep(0.05)

        observer = asyncio.create_task(observe_verifying())
        try:
            result = await self.controller.restore_and_verify(
                timeout=self.settings.verification_timeout_seconds)
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        # Late verification from an invalidated run cannot resolve it.
        if not self._is_current(run):
            return
        summary = VerificationSummary(
            verified=result.verified, reason=result.reason,
            duration_seconds=result.duration_seconds,
            baseline_rss_bytes=result.baseline_rss_bytes,
            rss_bytes=result.rss_bytes,
            retained_cache_bytes=result.retained_cache_bytes,
            completed_jobs=result.completed_jobs,
            failed_jobs=result.failed_jobs, generation=result.generation)
        run.verification = summary
        await self._emit("verification", summary.model_dump(mode="json"),
                         run_id=run.run_id)
        if result.verified:
            await self._set_phase(run, Phase.RESOLVED)
        else:
            await self._fail_run(
                run, "verification_failed",
                "Worker verification did not confirm recovery.")

    # -- reset --------------------------------------------------------------
    async def reset(self) -> StateSnapshot:
        async with self._op_lock:
            run = self._current_run
            # Invalidate BEFORE awaiting anything: late results are dropped.
            self._current_run = None
            if run is not None:
                run.cancel_event.set()
                await self._emit(
                    "phase_changed",
                    {"from": run.phase.value, "to": Phase.CANCELLED.value},
                    run_id=run.run_id)
                if run.task is not None and not run.task.done():
                    run.task.cancel()
                    await asyncio.gather(run.task, return_exceptions=True)
            try:
                await self.controller.reset()
                self._started = True
                if self._telemetry_task is None:
                    self._telemetry_task = asyncio.create_task(
                        self._telemetry_loop())
            except Exception:
                self._idle_phase = Phase.ERROR
                self._idle_error = ErrorBody(
                    code="reset_failed",
                    message="Worker reset failed; backend is not ready.")
                await self._emit(
                    "error",
                    {"code": "reset_failed",
                     "message": "Worker reset failed; backend is not ready."},
                    run_id=None)
                raise ApiError(
                    500, "reset_failed",
                    "Worker reset failed; backend is not ready.")
            self._idle_phase = Phase.IDLE
            self._idle_error = None
            snapshot = self.snapshot()
            await self._emit(
                "snapshot", snapshot.model_dump(mode="json"), run_id=None)
            return snapshot

