"""Bounded Baseten swarm orchestrator (AGENT-001).

Four specialists run concurrently against the same fixed model, then —
only when all four validated findings are complete — one synthesis call.
Any specialist failure cancels and awaits its siblings; no synthesis and
no remediation follow a partial result. The manager remains the only
authority that executes actions or verification. This orchestrator is
NOT wired into the default app in this task.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from aura_api.orchestration import (
    AgentEventSink,
    EvidenceBundle,
    OrchestrationOutcome,
    OrchestratorUnavailableError,
    SpecialistFinding,
    SynthesisProposal,
)
from aura_api.schemas import SPECIALIST_AGENT_IDS

from .adapter import MODEL, BasetenAdapter
from .prompts import (
    build_specialist_messages,
    build_synthesis_messages,
    parse_specialist_output,
    parse_synthesis_output,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CHART_BYTES = 5 * 1024 * 1024
DEFAULT_DIAGNOSIS_TIMEOUT_SECONDS = 120.0
ChartRenderer = Callable[[EvidenceBundle], Any]


class SwarmError(RuntimeError):
    def __init__(self, category: str):
        super().__init__(f"swarm diagnosis failed: {category}")
        self.category = category


async def _render_chart(renderer: ChartRenderer,
                        evidence: EvidenceBundle) -> bytes:
    try:
        rendered = renderer(evidence)
        if inspect.isawaitable(rendered):
            rendered = await rendered
    except Exception:
        raise SwarmError("chart_renderer_failed") from None
    if not isinstance(rendered, (bytes, bytearray)):
        raise SwarmError("chart_invalid")
    chart = bytes(rendered)
    if len(chart) > MAX_CHART_BYTES or not chart.startswith(PNG_SIGNATURE):
        raise SwarmError("chart_invalid")
    return chart


class BasetenSwarmOrchestrator:
    """Orchestrator protocol implementation backed by BasetenAdapter."""

    simulated = False

    def __init__(
        self,
        adapter: Optional[BasetenAdapter] = None,
        *,
        chart_renderer: Optional[ChartRenderer] = None,
        api_key: Optional[str] = None,
        project_root: Optional[Path] = None,
        evidence_dir: Optional[Path] = None,
        diagnosis_timeout_seconds: float = DEFAULT_DIAGNOSIS_TIMEOUT_SECONDS,
    ) -> None:
        self._adapter = adapter or BasetenAdapter(
            api_key=api_key, project_root=project_root)
        self._chart_renderer = chart_renderer
        self._evidence_dir = evidence_dir
        self._diagnosis_timeout = diagnosis_timeout_seconds
        self.call_metadata: list[dict] = []

    @property
    def ready(self) -> bool:
        # Missing key or missing Codex-owned chart renderer keeps the
        # orchestrator unready before any fault is injected.
        return bool(self._adapter.ready and self._chart_renderer is not None)

    @property
    def dispatched_counts(self) -> dict:
        return {"calls": self._adapter.dispatched_calls,
                "attempts": self._adapter.dispatched_attempts}

    async def aclose(self) -> None:
        await self._adapter.aclose()

    async def _run_specialist(
        self, agent_id: str, evidence: EvidenceBundle, chart: bytes,
        cancel_event: asyncio.Event, sink: AgentEventSink,
    ) -> SpecialistFinding:
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        await sink.agent_started(agent_id)
        try:
            messages, allowed_refs = build_specialist_messages(
                evidence, agent_id)
            if agent_id == "vision":
                data_url = "data:image/png;base64," + base64.b64encode(
                    chart).decode("ascii")
                messages[1] = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": messages[1]["content"]},
                        {"type": "image_url",
                         "image_url": {"url": data_url, "detail": "auto"}},
                    ],
                }
            result = await self._adapter.complete(
                messages, cancel_event=cancel_event)
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            # Record trusted call metadata (incl. usage) before parsing,
            # so it survives a later validation failure of the content.
            self.call_metadata.append({
                "agent_id": agent_id, "requested_model": MODEL,
                "response_model": result.response_model,
                "duration_ms": result.duration_ms,
                "attempts": result.attempts,
                "usage": result.usage.model_dump() if result.usage else None,
                "output_excerpt": (result.content or "")[:2000],
            })
            parsed = parse_specialist_output(
                result.content, result.finish_reason, agent_id, allowed_refs)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not cancel_event.is_set():
                await sink.agent_finished(agent_id, "failed")
            raise
        finding = SpecialistFinding(
            agent_id=agent_id, status="completed",
            summary=parsed["summary"], findings=parsed["findings"],
            evidence_refs=parsed["evidence_refs"],
            reasoning=result.reasoning,
            reasoning_truncated=result.reasoning_truncated,
            usage=result.usage,
            model=result.response_model,
            requested_model=result.requested_model,
            response_model=result.response_model,
            duration_ms=result.duration_ms, attempts=result.attempts)
        if result.reasoning:
            await sink.agent_message(agent_id, result.reasoning,
                                     kind="reasoning")
        await sink.agent_message(agent_id, finding.summary, kind="finding")
        await sink.agent_finished(agent_id, "completed", finding.summary)
        return finding

    async def diagnose(
        self,
        evidence: EvidenceBundle,
        cancel_event: asyncio.Event,
        event_sink: AgentEventSink,
    ) -> OrchestrationOutcome:
        """Full diagnosis under one finite overall budget (120 s default).

        Five calls normally; at most ten attempts with the single bounded
        retry per call. Exhaustion cancels every child before returning.
        """
        try:
            return await asyncio.wait_for(
                self._diagnose_inner(evidence, cancel_event, event_sink),
                timeout=self._diagnosis_timeout)
        except asyncio.TimeoutError:
            self._save_failure_record(
                evidence, "diagnosis_timeout")
            raise SwarmError("diagnosis_timeout") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._save_failure_record(
                evidence, str(getattr(exc, "category", "swarm_failed")))
            raise

    async def _diagnose_inner(
        self,
        evidence: EvidenceBundle,
        cancel_event: asyncio.Event,
        event_sink: AgentEventSink,
    ) -> OrchestrationOutcome:
        if not self.ready:
            raise OrchestratorUnavailableError(
                "provider key or chart renderer is not configured")
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        self.call_metadata = []
        # Chart first: a renderer failure means zero model dispatch.
        chart = await _render_chart(self._chart_renderer, evidence)  # type: ignore[arg-type]
        if cancel_event.is_set():
            raise asyncio.CancelledError()

        tasks = [
            asyncio.create_task(self._run_specialist(
                agent_id, evidence, chart, cancel_event, event_sink))
            for agent_id in SPECIALIST_AGENT_IDS
        ]
        try:
            findings = await asyncio.gather(*tasks)
        except BaseException:
            # Cancel AND await every sibling; nothing runs in background.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        by_id = {finding.agent_id: finding for finding in findings}
        ordered = [by_id[agent_id] for agent_id in SPECIALIST_AGENT_IDS]

        await event_sink.agent_started("synthesis")
        messages, allowed_refs = build_synthesis_messages(evidence, ordered)
        try:
            result = await self._adapter.complete(
                messages, cancel_event=cancel_event)
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            self.call_metadata.append({
                "agent_id": "synthesis", "requested_model": MODEL,
                "response_model": result.response_model,
                "duration_ms": result.duration_ms,
                "attempts": result.attempts,
                "usage": result.usage.model_dump() if result.usage else None,
            })
            parsed = parse_synthesis_output(
                result.content, result.finish_reason, allowed_refs)
        except asyncio.CancelledError:
            raise
        except Exception:
            await event_sink.agent_finished("synthesis", "failed")
            raise
        proposal = SynthesisProposal(
            action=parsed["action"], reason=parsed["reason"],
            evidence_refs=parsed["evidence_refs"],
            reasoning=result.reasoning,
            reasoning_truncated=result.reasoning_truncated,
            usage=result.usage,
            model=result.response_model,
            requested_model=result.requested_model,
            response_model=result.response_model,
            duration_ms=result.duration_ms, attempts=result.attempts)
        if result.reasoning:
            await event_sink.agent_message(
                "synthesis", result.reasoning, kind="reasoning")
        await event_sink.agent_finished(
            "synthesis", "completed", proposal.reason)
        outcome = OrchestrationOutcome(findings=ordered, proposal=proposal)
        self._save_evidence_record(evidence, outcome)
        return outcome

    def _save_failure_record(self, evidence: EvidenceBundle,
                             category: str) -> None:
        """Bounded failure record incl. usage of already-answered calls."""
        if self._evidence_dir is None:
            return
        record = {
            "run_id": evidence.run_id, "scenario": evidence.scenario,
            "recorded_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error_category": category[:64],
            "call_metadata": self.call_metadata,
        }
        try:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
            safe_run = "".join(
                ch for ch in evidence.run_id if ch.isalnum() or ch in "-_")
            path = self._evidence_dir / f"run-{safe_run or 'unknown'}.json"
            path.write_text(
                json.dumps(record, indent=2)[:65536], encoding="utf-8")
        except OSError:
            pass

    def _save_evidence_record(self, evidence: EvidenceBundle,
                              outcome: OrchestrationOutcome) -> None:
        """Bounded run record under .local/agents only; no secrets/headers."""
        if self._evidence_dir is None:
            return
        record = {
            "run_id": evidence.run_id, "scenario": evidence.scenario,
            "recorded_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "call_metadata": self.call_metadata,
            "findings": [
                {"agent_id": finding.agent_id, "status": finding.status,
                 "summary": finding.summary[:500],
                 "evidence_refs": list(finding.evidence_refs)}
                for finding in outcome.findings
            ],
            "proposal": {
                "action": outcome.proposal.action,
                "reason": outcome.proposal.reason[:500],
            },
        }
        try:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
            safe_run = "".join(
                ch for ch in evidence.run_id if ch.isalnum() or ch in "-_")
            path = self._evidence_dir / f"run-{safe_run or 'unknown'}.json"
            path.write_text(
                json.dumps(record, indent=2)[:65536], encoding="utf-8")
        except OSError:
            pass
