"""Dependency-injected orchestration interface for the next agent task.

No model implementation lives here. The shipped default orchestrator is
unavailable; deterministic fakes exist only in tests, injected through
the Python app factory (no HTTP/header/environment selection switch).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .schemas import SPECIALIST_AGENT_IDS, AgentUsage


class SpecialistFinding(BaseModel):
    """Structured result contract for one specialist agent."""

    model_config = ConfigDict(extra="forbid")

    agent_id: Literal["logs", "memory", "config", "vision"]
    status: Literal["completed", "failed", "incomplete"]
    summary: str = Field(max_length=2000)
    findings: list[str] = Field(default_factory=list, max_length=32)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    reasoning: Optional[str] = Field(default=None, max_length=8000)
    reasoning_truncated: bool = False
    usage: Optional[AgentUsage] = None
    model: Optional[str] = Field(default=None, max_length=256)
    # Trustworthy call metadata set by the orchestrator, never by model
    # output content: requested model, observed response model, timing.
    requested_model: Optional[str] = Field(default=None, max_length=256)
    response_model: Optional[str] = Field(default=None, max_length=256)
    duration_ms: Optional[float] = None
    attempts: int = 0


class SynthesisProposal(BaseModel):
    """Validated synthesis output; action is a strict allowlist."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["restore_cache_policy", "abstain"]
    reason: str = Field(max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    reasoning: Optional[str] = Field(default=None, max_length=8000)
    reasoning_truncated: bool = False
    usage: Optional[AgentUsage] = None
    model: Optional[str] = Field(default=None, max_length=256)
    requested_model: Optional[str] = Field(default=None, max_length=256)
    response_model: Optional[str] = Field(default=None, max_length=256)
    duration_ms: Optional[float] = None
    attempts: int = 0


class OrchestrationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[SpecialistFinding] = Field(max_length=8)
    proposal: SynthesisProposal


@dataclass
class EvidenceBundle:
    """Bounded measured evidence handed to the orchestrator."""

    run_id: str
    scenario: str
    worker_generation: Optional[int]
    measured_at: Optional[str]
    baseline_rss_bytes: Optional[int]
    rss_bytes: Optional[int]
    rss_fresh: bool
    retained_cache_bytes: Optional[int]
    healthy_cache_bytes: int
    ceiling_bytes: int
    rss_threshold_bytes: Optional[int]
    completed_jobs: Optional[int]
    failed_jobs: Optional[int]
    worker_state: str
    recent_measurements: list[dict] = field(default_factory=list)
    # Provenance-labelled actual worker configuration (from worker status).
    worker_config: dict = field(default_factory=dict)
    worker_config_source: str = "unavailable"
    eviction_enabled: Optional[bool] = None
    # Status freshness at evidence-build time.
    status_fresh: bool = False
    status_timestamp: Optional[str] = None
    # Current-run sampled telemetry (same samples as recent_measurements,
    # labelled as sampled controller telemetry, not worker logs).
    sampled_telemetry: list[dict] = field(default_factory=list)
    # Actual diagnostic log lines when the controller exposes them. When
    # empty, no per-request worker logs exist; counter-derived summaries
    # must be labelled derived, never verbatim logs.
    diagnostic_logs: list[dict] = field(default_factory=list)
    diagnostic_logs_source: str = "unavailable"
    # IDs specialists may cite in evidence_refs.
    evidence_ids: list[str] = field(default_factory=list)


@runtime_checkable
class AgentEventSink(Protocol):
    """Sink the orchestrator may report progress through (run-scoped)."""

    async def agent_started(self, agent_id: str) -> None: ...

    async def agent_message(self, agent_id: str, message: str,
                            kind: str = "status") -> None: ...

    async def agent_finished(self, agent_id: str, status: str,
                             summary: Optional[str] = None) -> None: ...


@runtime_checkable
class Orchestrator(Protocol):
    """Interface the next task implements with real provider agents."""

    ready: bool
    simulated: bool

    async def diagnose(
        self,
        evidence: EvidenceBundle,
        cancel_event: asyncio.Event,
        event_sink: AgentEventSink,
    ) -> OrchestrationOutcome: ...


class OrchestratorUnavailableError(RuntimeError):
    pass


class UnavailableOrchestrator:
    """Default shipped orchestrator: never fabricates a diagnosis."""

    ready = False
    simulated = False

    async def diagnose(
        self,
        evidence: EvidenceBundle,
        cancel_event: asyncio.Event,
        event_sink: AgentEventSink,
    ) -> OrchestrationOutcome:
        raise OrchestratorUnavailableError("orchestrator is not implemented")


def validate_outcome(raw: Any) -> OrchestrationOutcome:
    """Parse and structurally validate an orchestrator outcome."""
    if isinstance(raw, OrchestrationOutcome):
        data = raw.model_dump()
    elif isinstance(raw, BaseModel):
        data = raw.model_dump()
    else:
        data = raw
    return OrchestrationOutcome.model_validate(data)


def outcome_completeness(outcome: OrchestrationOutcome) -> Optional[str]:
    """Return None when findings are complete, else a safe reason code."""
    by_id = {finding.agent_id: finding for finding in outcome.findings}
    if set(by_id) != set(SPECIALIST_AGENT_IDS) or len(outcome.findings) != 4:
        return "incomplete_specialist_findings"
    for agent_id in SPECIALIST_AGENT_IDS:
        finding = by_id[agent_id]
        if finding.status != "completed" or not finding.summary.strip() \
                or not finding.findings:
            return "incomplete_specialist_findings"
    return None
