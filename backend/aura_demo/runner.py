"""One bounded live run through the ordinary OpsService and model adapter."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aura_agents.adapter import BasetenAdapter, MODEL
from aura_ops.docker import DockerTarget
from aura_ops.models import OpsError, Policy
from aura_ops.investigator import OpsInvestigator
from aura_ops.redaction import redact_text
from aura_ops.service import OpsService
from aura_ops.store import OpsStore
from .fixture import LinkdingFixture


class BudgetAdapter:
    def __init__(self, delegate, ledger, run_id):
        self.delegate, self.ledger, self.run_id = delegate, ledger, run_id
        self.records = []
        self.closed = False

    @property
    def ready(self):
        return not self.closed and self.delegate.ready

    async def complete(self, messages, **options):
        if self.closed or options.get("cancel_event") and options["cancel_event"].is_set():
            raise asyncio.CancelledError()
        # Also cap input bytes; not a token-price or dollar-billing estimate.
        if len(json.dumps(messages, ensure_ascii=False).encode()) > 160_000:
            raise OpsError("demo_input_limit", "The investigation input limit was reached.", 429)
        if options.get("max_tokens", 4096) > 4096:
            raise OpsError("demo_output_limit", "The investigation output limit was reached.", 429)
        self.ledger.reserve_call(self.run_id)
        result = await self.delegate.complete(messages, **options)
        self.records.append({"model": result.requested_model, "duration_ms": result.duration_ms,
                             "attempts": result.attempts,
                             "usage": result.usage.model_dump() if result.usage else None})
        return result

    async def aclose(self):
        self.closed = True
        await self.delegate.aclose()


def public_observation(observation):
    resources = observation.get("resources") or {}
    return {"timestamp": observation.get("timestamp"), "healthy": observation.get("healthy"),
            "memory_bytes": resources.get("memory_bytes"),
            "probes": [{"name": "Bookmarks" if "/api/bookmarks/" in p.get("url", "") else "Health",
                        "ok": p.get("ok"), "status_code": p.get("status_code"),
                        "duration_ms": p.get("duration_ms")} for p in observation.get("probes", [])]}


class DemoRunner:
    def __init__(self, settings, ledger, *, adapter_factory=None, fixture_factory=LinkdingFixture):
        self.settings, self.ledger = settings, ledger
        self.adapter_factory = adapter_factory or (lambda: BasetenAdapter(project_root=settings.root))
        self.fixture_factory = fixture_factory
        self.task = None
        self.run_id = None
        self.cancel_requested = False

    def ready(self):
        # Presence check only, no request or client construction.
        from aura_api.config import detect_provider_configured
        return detect_provider_configured(self.settings.root)

    async def startup(self):
        # Interrupted disposable runs are never resumed and never declared successful.
        # Only their labelled resources may be cleaned; reservations remain consumed.
        for row in self.ledger.active():
            fixture = self.fixture_factory(self.settings, self.ledger.owner, row["id"])
            failures = await fixture.cleanup()
            report = json.loads(row["report"])
            report.update(phase="interrupted", error="The server restarted. This run was stopped.", verified=False,
                          cleanup_complete=not failures)
            self.ledger.save(row["id"], report, finished=True, blocked=bool(failures))

    def launch(self, run_id):
        if self.task and not self.task.done():
            raise OpsError("demo_busy", "Another recovery is running.", 409)
        self.run_id = run_id
        self.cancel_requested = False
        self.task = asyncio.create_task(self.run(run_id))

    async def cancel(self, run_id):
        if self.run_id == run_id and self.task and not self.task.done():
            # Set once: repeated cancel requests must not interrupt cleanup.
            if not self.cancel_requested:
                self.cancel_requested = True
                self.task.cancel()
            await asyncio.shield(self.task)

    async def shutdown(self):
        if self.task and not self.task.done():
            await self.cancel(self.run_id)

    async def run(self, run_id):
        fixture = self.fixture_factory(self.settings, self.ledger.owner, run_id)
        report = json.loads(self.ledger.row(run_id)["report"])
        adapter = BudgetAdapter(self.adapter_factory(), self.ledger, run_id)
        service = None
        started = time.time()
        fault_at = None

        def save(phase=None, **changes):
            if phase and phase != report["phase"]:
                report["events"].append({"phase": phase, "at": time.time()})
                report["phase"] = phase
            report.update(changes)
            report["calls"] = adapter.records
            report["reserved_calls"] = self.ledger.row(run_id)["calls"]
            report["elapsed_seconds"] = round(time.time() - (fault_at or started), 2)
            self.ledger.save(run_id, report)

        async def scenario():
            nonlocal service, fault_at
            if not adapter.ready:
                raise OpsError("provider_unavailable", "The model connection is not configured.", 503)
            spec = await fixture.provision()
            service = OpsService(self.settings.root, store=OpsStore(fixture.folder / "ops"),
                                 investigator=OpsInvestigator(adapter))
            await service.startup()
            await service.enroll(spec)
            save("checking_baseline")
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                observation = service.observations.get(spec.target_id) or {}
                if observation.get("healthy") is True:
                    break
                await asyncio.sleep(1)
            else:
                raise OpsError("baseline_unhealthy", "The test app did not become healthy. No fault was introduced.", 503)
            await service.capture_snapshot(spec.target_id)
            await service.set_policy(spec.target_id, Policy(auto_investigate=True,
                                     automatic_actions=["restore_config_snapshot"]))
            save("healthy", observation=public_observation(observation))
            # Three healthy observations establish the control before changing anything.
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                history = list(service.histories.get(spec.target_id, []))
                if len(history) >= 3 and all(s.get("healthy") is True for s in history[-3:]):
                    break
                await asyncio.sleep(1)
            else:
                raise OpsError("baseline_unstable", "The test app was not stable before the run.", 503)
            if service.store.list_investigations() or adapter.records:
                raise OpsError("baseline_incident", "An unexpected issue occurred before the test.", 503)
            fault_at = time.time()
            save("introducing_problem", fault_started_at=fault_at)
            await fixture.fault()
            save("detecting", fault_confirmed=True)
            last_sample = None
            while True:
                if self.cancel_requested:
                    raise asyncio.CancelledError()
                observed = service.observations.get(spec.target_id) or {}
                sample = public_observation(observed)
                if sample["timestamp"] and sample["timestamp"] != last_sample:
                    report["samples"] = (report["samples"] + [sample])[-160:]
                    last_sample = sample["timestamp"]
                rows = service.store.list_investigations()
                if len(rows) > 1:
                    raise OpsError("demo_investigation_limit", "Only one investigation is allowed per demonstration.", 429)
                row = rows[0] if rows else None
                changes = {"observation": sample}
                if row:
                    changes["investigation_id"] = row["investigation_id"]
                    diagnosis = row.get("diagnosis") or {}
                    if diagnosis:
                        changes["diagnosis"] = {key: redact_text(str(diagnosis.get(key, "")))[:1500]
                                                for key in ("summary", "reason", "action")}
                    events = service.store.events_since(0)
                    changes["agents"] = [{"type": e["type"], "agent": e["payload"].get("agent_id"),
                                          "summary": redact_text(str(e["payload"].get("summary", "")))[:600]}
                                         for e in events if e["type"] in ("agent_started", "agent_finished")][-14:]
                    save(row["phase"], **changes)
                    if row["phase"] in ("mitigated", "needs_human", "cancelled"):
                        await service.set_policy(spec.target_id, Policy())
                        verification = row.get("verification") or {}
                        proposals = service.store.list_proposals()
                        measured = await DockerTarget(spec).collect()
                        actual = await fixture.app()
                        verified = (row["phase"] == "mitigated" and verification.get("verified") is True
                                    and measured.get("healthy") is True and diagnosis.get("action") == "restore_config_snapshot"
                                    and "LD_SERVER_PORT=9090" in actual["Config"].get("Env", [])
                                    and len(proposals) == 1 and proposals[0].get("automatic") is True
                                    and proposals[0].get("authorization") == "policy"
                                    and bool(proposals[0].get("dispatch_attempted_at")) and bool(adapter.records))
                        save("recovered" if verified else "needs_help", verified=verified,
                             observation=public_observation(measured),
                             verification={k: verification.get(k) for k in
                                           ("sample_count", "incomplete_samples", "duration_seconds", "kind", "root_cause_fixed")},
                             error=None if verified else "AURA could not verify an automatic recovery in this run.")
                        report["recovery_seconds"] = round(time.time() - fault_at, 3) if verified else None
                        break
                    if row["phase"] == "awaiting_approval" and service._task and service._task.done():
                        save("needs_help", error="AURA requested human review. The demo will not approve a different action.")
                        break
                else:
                    save(**changes)
                await asyncio.sleep(1)

        try:
            await asyncio.wait_for(scenario(), self.settings.deadline)
        except asyncio.CancelledError:
            save("cancelled", verified=False, error="This run was stopped. No recovery is claimed.")
        except asyncio.TimeoutError:
            save("failed", verified=False, error="The run reached its time limit. No recovery was confirmed.")
        except Exception as exc:
            # No raw Docker/model exception, request or response is exposed.
            save("failed", verified=False, error="The live demonstration could not complete.",
                 error_code=exc.code if isinstance(exc, OpsError) else "demo_failed")
        finally:
            shutdown_failed = False
            try:
                if service:
                    await service.shutdown()
                else:
                    await adapter.aclose()
            except Exception:
                shutdown_failed = True
            failures = await fixture.cleanup()
            if shutdown_failed:
                failures.append("controller_shutdown")
            report.update(calls=adapter.records, cleanup_complete=not failures,
                          finished_at=time.time(), model=MODEL)
            if failures:
                report["cleanup_error"] = "The test environment needs operator attention before another run."
            self.ledger.save(run_id, report, finished=True, blocked=bool(failures))
