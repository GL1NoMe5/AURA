"""Independent restart regressions for OPS-LIFECYCLE-GUARD-001 (offline)."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops_service import make_service, observed, target
from test_ops_lifecycle_guards import source_target, make_diagnosis_row, REVISION


async def idle_monitor():
    """Startup bookkeeping only: do not probe Docker or schedule diagnosis."""
    await asyncio.Event().wait()


def test_interrupted_startup_cannot_lose_executed_unknown_gate(tmp_path):
    async def run():
        service = make_service(tmp_path)
        service._monitor = idle_monitor
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Interrupted recovery")
            proposal = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": row["investigation_id"],
                "action": "restart_container", "status": "executed",
                "result": {"command_completed": True, "identity": observed()["identity"]},
            })
            service.store.update_investigation(
                row["investigation_id"], phase="verifying", proposal_id=proposal["proposal_id"])
            original = service.store.update_proposal

            def interrupt_before_gate(proposal_id, **changes):
                if proposal_id == proposal["proposal_id"] and changes.get("status") == "unknown":
                    raise RuntimeError("simulated process loss before gate commit")
                return original(proposal_id, **changes)

            service.store.update_proposal = interrupt_before_gate
            with pytest.raises(RuntimeError, match="simulated process loss"):
                await service.startup()
            assert service.investigator.calls == []
        finally:
            await service.shutdown()

        # A second process must classify the still-unverified operation even if
        # the previous process committed the investigation repair first.
        restarted = make_service(tmp_path)
        restarted._monitor = idle_monitor
        try:
            await restarted.startup()
            stored = restarted.store.get_proposal(proposal["proposal_id"])
            assert stored["status"] == "unknown"
            assert stored["result"]["command_completed"] is True
            assert [p["proposal_id"] for p in restarted.store.list_unresolved_unknowns("alpha")] == [proposal["proposal_id"]]
            assert restarted.investigator.calls == []
            assert restarted._task is None
        finally:
            await restarted.shutdown()
    asyncio.run(run())


def seed_verified_proposal_before_phase_commit(service, tmp_path, fault=None):
    row = make_diagnosis_row(service.store, "alpha", phase="verifying")
    patch = {"patch_id": "b2" * 16, "target_id": "alpha",
             "investigation_id": row["investigation_id"],
             "source_revision": REVISION, "status": "tests_passed"}
    identity = observed(image="c")["identity"]
    verification = {"verified": True, "root_cause_fixed": True,
                    "kind": "tested_patch_and_runtime",
                    "availability": {"verified": True},
                    "functional": {"verified": True, "identity": deepcopy(identity)}}
    proposal = {"target_id": "alpha", "investigation_id": row["investigation_id"],
                "action": "deploy_verified_patch", "status": "verified",
                "patch_id": patch["patch_id"], "proposal_hash": "a" * 64,
                "approved_hash": "a" * 64, "authorization": "user",
                "deployment_dispatched": True,
                "dispatch_attempted_at": "2026-10-29T00:00:00Z",
                "candidate_image_id": identity["image_id"],
                "deployment_identity": identity,
                "result": {"command_completed": True, "identity": deepcopy(identity)},
                "verification": deepcopy(verification)}
    if fault == "target":
        proposal["target_id"] = "beta"
    elif fault == "patch":
        proposal["patch_id"] = "c3" * 16
    elif fault == "functional":
        proposal["verification"]["functional"]["verified"] = False
    stored = service.store.put_proposal(proposal)
    service.store.update_investigation(row["investigation_id"], patch=patch,
                                      proposal_id=stored["proposal_id"])
    return row, stored, verification


def test_restart_preserves_verified_deployment_before_investigation_phase_commit(tmp_path):
    async def run():
        service = make_service(tmp_path)
        service._monitor = idle_monitor
        try:
            await service.enroll(source_target(tmp_path))
            row, proposal, verification = seed_verified_proposal_before_phase_commit(service, tmp_path)
            # This is _deploy's real commit order: the proposal contains the
            # successful result before phase(verified_fixed) updates the run.
            await service.startup()
            restored = service.investigation(row["investigation_id"])
            assert restored["phase"] == "verified_fixed"
            assert restored["verification"] == verification
            assert restored["proposal_id"] == proposal["proposal_id"]
            assert not restored.get("baseline_promoted")
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == "verified"
            assert service.investigator.calls == []
            assert service._task is None
        finally:
            await service.shutdown()

        restarted = make_service(tmp_path)
        restarted._monitor = idle_monitor
        try:
            await restarted.startup()
            assert restarted.investigation(row["investigation_id"])["phase"] == "verified_fixed"
            assert restarted.store.list_unresolved_unknowns("alpha") == []
            assert restarted.investigator.calls == []
        finally:
            await restarted.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["target", "patch", "functional"])
def test_restart_does_not_restore_unbound_or_unverified_fix(tmp_path, fault):
    async def run():
        service = make_service(tmp_path)
        service._monitor = idle_monitor
        try:
            await service.enroll(source_target(tmp_path))
            row, proposal, _ = seed_verified_proposal_before_phase_commit(service, tmp_path, fault)
            await service.startup()
            restored = service.investigation(row["investigation_id"])
            assert restored["phase"] == "needs_human"
            assert restored.get("verification") is None
            assert not restored.get("baseline_promoted")
            assert service.investigator.calls == []
            assert service._task is None
        finally:
            await service.shutdown()
    asyncio.run(run())
