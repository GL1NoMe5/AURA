"""Typed HTTP/WebSocket schemas for the AURA local API (API-001)."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SPECIALIST_AGENT_IDS = ("logs", "memory", "config", "vision")
ALL_AGENT_IDS = SPECIALIST_AGENT_IDS + ("synthesis",)


class Phase(str, Enum):
    IDLE = "idle"
    FAULT_INJECTED = "fault_injected"
    DIAGNOSING = "diagnosing"
    REMEDIATING = "remediating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ERROR = "error"
    CANCELLED = "cancelled"


TERMINAL_PHASES = frozenset({Phase.RESOLVED, Phase.ERROR, Phase.CANCELLED})


class IncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["cache_retention"]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class TelemetrySnapshot(BaseModel):
    rss_bytes: Optional[int] = None
    baseline_rss_bytes: Optional[int] = None
    retained_cache_bytes: Optional[int] = None
    completed_jobs: Optional[int] = None
    failed_jobs: Optional[int] = None
    sample_timestamp: Optional[str] = None
    status_timestamp: Optional[str] = None
    generation: Optional[int] = None
    rss_fresh: bool = False
    status_fresh: bool = False
    worker_state: str = "unknown"
    worker_health: str = "unknown"


class AgentSummary(BaseModel):
    agent_id: str
    status: Literal["idle", "running", "completed", "failed"] = "idle"
    summary: Optional[str] = None
    findings: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    reasoning: Optional[str] = None
    reasoning_truncated: bool = False
    usage: Optional["AgentUsage"] = None
    requested_model: Optional[str] = None
    response_model: Optional[str] = None
    duration_ms: Optional[float] = None
    attempts: int = 0


class AgentUsage(BaseModel):
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None


class VerificationSummary(BaseModel):
    verified: bool
    reason: str
    duration_seconds: float = 0.0
    baseline_rss_bytes: Optional[int] = None
    rss_bytes: Optional[int] = None
    retained_cache_bytes: Optional[int] = None
    completed_jobs: Optional[int] = None
    failed_jobs: Optional[int] = None
    generation: Optional[int] = None


class StateSnapshot(BaseModel):
    seq: int = 0
    run_id: Optional[str] = None
    phase: Phase = Phase.IDLE
    scenario: Optional[str] = None
    server_instance_id: str
    worker_health: str = "unknown"
    telemetry: TelemetrySnapshot = Field(default_factory=TelemetrySnapshot)
    agents: list[AgentSummary] = Field(default_factory=list)
    latest_verification: Optional[VerificationSummary] = None
    proposal: Optional[dict] = None
    error: Optional[ErrorBody] = None
    simulated_diagnosis: bool = False


class HealthResponse(BaseModel):
    status: str
    provider_configured: bool
    orchestrator_ready: bool
    worker_state: str
    worker_running: bool
    server_instance_id: str


class IncidentAccepted(BaseModel):
    run_id: str
    phase: Phase
    scenario: str


class EventEnvelope(BaseModel):
    type: str
    run_id: Optional[str] = None
    seq: int
    timestamp: str
    payload: dict[str, Any] = Field(default_factory=dict)
    server_instance_id: str
