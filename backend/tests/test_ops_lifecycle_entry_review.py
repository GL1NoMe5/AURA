"""Independent lifecycle entry regressions; temporary stores and fake boundaries only."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aura_ops.models import OpsError, TargetSpec
from test_ops_lifecycle_guards import (
    FakePatchEngine, REVISION, make_diagnosis_row, source_target,
)
from test_ops_service import make_service, observed, until


@pytest.mark.parametrize("interruption", ["failure", "cancelled"])
def test_retained_tested_candidate_can_retry_build_without_regeneration(tmp_path, interruption):
    async def run():
        service = make_service(tmp_path, patch_engine=FakePatchEngine())
        try:
            spec = source_target(tmp_path)
            spec = TargetSpec.model_validate({
                **spec.model_dump(mode="json"),
                "recovery_checks": [{"url": "http://127.0.0.1:18090/health",
                                     "expected_status": 200, "expected_bytes": 10,
                                     "expected_sha256": "a" * 64}],
            })
            await service.enroll(spec)
            row = make_diagnosis_row(service.store, "alpha", phase="fix_proposed")
            patch = {"patch_id": "a" * 32, "target_id": "alpha",
                     "investigation_id": row["investigation_id"], "status": "tests_passed",
                     "source_revision": REVISION, "completed_at": "2026-10-29T00:00:00Z"}
            service.store.update_investigation(row["investigation_id"], patch=deepcopy(patch))
            entered, hold = asyncio.Event(), asyncio.Event()
            builds = []

            async def build(spec_arg, investigation_id, patch_arg, observation):
                builds.append(investigation_id)
                assert patch_arg == patch
                if len(builds) == 1:
                    entered.set()
                    if interruption == "failure":
                        raise OpsError("temporary_build_failure", "Retry this build", 503)
                    await hold.wait()
                return service.store.put_proposal({
                    "target_id": "alpha", "investigation_id": investigation_id,
                    "patch_id": patch["patch_id"], "action": "deploy_verified_patch",
                    "status": "proposed", "proposal_hash": "b" * 64,
                })

            service.deployment_manager.build_proposal = build
            await service.prepare_deployment(row["investigation_id"])
            await entered.wait()
            if interruption == "cancelled":
                await service.cancel(row["investigation_id"])
            await until(lambda: service._task is not None and service._task.done())
            retained = service.investigation(row["investigation_id"])
            assert retained["patch"] == patch
            assert service.store.list_proposals(row["investigation_id"]) == []
            # Retrying must reuse the tested candidate, not generate a replacement.
            with pytest.raises(OpsError) as rejected:
                await service.prepare_patch(row["investigation_id"])
            assert rejected.value.code == "fix_already_proposed"
            await service.prepare_deployment(row["investigation_id"])
            await until(lambda: service._task is not None and service._task.done())
            pending = service.investigation(row["investigation_id"])
            assert pending["phase"] == "awaiting_approval"
            assert pending["patch"] == patch
            proposal = service.store.get_proposal(pending["proposal_id"])
            assert proposal["status"] == "proposed"
            assert proposal.get("authorization") is None
            assert builds == [row["investigation_id"], row["investigation_id"]]
            assert service.patch_engine.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["disconnect", "source", "completed"])
def test_patch_entry_rechecks_state_after_awaited_observation(tmp_path, change):
    async def run():
        engine = FakePatchEngine()
        service = make_service(tmp_path, patch_engine=engine)
        try:
            await service.enroll(source_target(tmp_path))
            row = make_diagnosis_row(service.store, "alpha", phase="needs_human")
            original_observe = service._observe
            changed_row = None

            async def observing(spec):
                nonlocal changed_row
                result = await original_observe(spec)
                if change == "disconnect":
                    # A real public entry point; it is not serialized by _lock.
                    await service.disconnect("alpha")
                elif change == "source":
                    enrolled = service.store.get_target("alpha")
                    enrolled["source_revision"] = "e" * 40
                    service.store.put_target(enrolled)
                else:
                    service.store.update_investigation(
                        row["investigation_id"], phase="verified_fixed",
                        verification={"verified": True, "root_cause_fixed": True},
                        baseline_error={"code": "promotion_failed", "message": "Preserve"},
                    )
                changed_row = deepcopy(service.investigation(row["investigation_id"]))
                return result

            service._observe = observing
            error = None
            try:
                await service.prepare_patch(row["investigation_id"])
            except OpsError as exc:
                error = exc
            if service._task is not None:
                await until(lambda: service._task.done())
            assert engine.calls == [], "Stale entry started the fake paid-work boundary"
            assert error is not None, "State change during preflight must reject preparation"
            assert service.investigation(row["investigation_id"]) == changed_row
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["needs_human", "cancelled"])
@pytest.mark.parametrize("fault", ["target", "source", "dispatch", "unknown"])
def test_tested_build_retry_rejects_bad_binding_or_prior_dispatch(tmp_path, phase, fault):
    async def run():
        service = make_service(tmp_path, patch_engine=FakePatchEngine())
        try:
            spec = source_target(tmp_path)
            spec = TargetSpec.model_validate({
                **spec.model_dump(mode="json"),
                "recovery_checks": [{"url": "http://127.0.0.1:18090/health",
                                     "expected_status": 200, "expected_bytes": 10,
                                     "expected_sha256": "a" * 64}],
            })
            await service.enroll(spec)
            row = make_diagnosis_row(service.store, "alpha", phase=phase)
            patch = {"patch_id": "c" * 32, "target_id": "alpha",
                     "investigation_id": row["investigation_id"], "status": "tests_passed",
                     "source_revision": REVISION, "completed_at": "2026-10-29T00:00:00Z"}
            if fault == "target":
                patch["target_id"] = "beta"
            elif fault == "source":
                patch["source_revision"] = "e" * 40
            service.store.update_investigation(row["investigation_id"], patch=patch)
            if fault in ("dispatch", "unknown"):
                service.store.put_proposal({
                    "target_id": "alpha", "investigation_id": row["investigation_id"],
                    "patch_id": patch["patch_id"], "action": "deploy_verified_patch",
                    "status": "unknown" if fault == "unknown" else "executed",
                    "result": {"command_completed": fault == "dispatch"},
                })
            before = deepcopy(service.investigation(row["investigation_id"]))
            proposals_before = deepcopy(service.store.list_proposals(row["investigation_id"]))
            builds = []

            async def forbidden_build(*args):
                builds.append(args)
                raise AssertionError("Invalid retained candidate must not reach build")

            service.deployment_manager.build_proposal = forbidden_build
            with pytest.raises(OpsError):
                await service.prepare_deployment(row["investigation_id"])
            assert builds == []
            assert service._task is None
            assert service.investigation(row["investigation_id"]) == before
            assert service.store.list_proposals(row["investigation_id"]) == proposals_before
            assert service.patch_engine.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())
