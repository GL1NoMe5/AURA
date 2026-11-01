"""OPS-OUTCOME-GUARD-001: unresolved unknown blocks new intervention (offline)."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aura_ops.actions import ActionExecutor
from aura_ops.deployment import DeploymentManager
from aura_ops.models import ActionKind, OpsError
from aura_ops.store import OpsStore
from test_ops_actions import FakeRunner, claim, create, expect_error, make_spec
from test_ops_service import Docker, Investigator, make_service, observed, target, until


@pytest.fixture
def store(tmp_path):
    instance = OpsStore(tmp_path / "ops")
    yield instance
    instance.close()


def put_unknown(store, target_id="bookmarks", proposal_id=None):
    return store.put_proposal({
        "proposal_id": proposal_id or f"unknown-{target_id}-{store.latest_seq()}",
        "target_id": target_id, "status": "unknown",
        "action": "restart_container", "error": "action_outcome_unknown",
    })


def test_unknown_blocks_manual_and_automatic_even_after_cooldown(store, tmp_path):
    async def scenario():
        for action in (ActionKind.RESTART, ActionKind.RESTORE, ActionKind.ROLLBACK):
            fresh = OpsStore(tmp_path / f"ops-{action.value}")
            try:
                spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
                executor = ActionExecutor(fresh, runner=runner)
                await executor.capture_snapshot(spec, runner.observation())
                if action == ActionKind.ROLLBACK:
                    from test_ops_actions import OTHER_IMAGE
                    runner.container["Image"] = OTHER_IMAGE
                    runner.container["Config"]["Image"] = "linkding:new-build"
                put_unknown(fresh, "bookmarks")
                fresh.put_setting("lastrestart.bookmarks", time.time() - 3600)
                manual = executor.create_proposal(spec, "inv-manual", {
                    "action": action.value, "reason": "Manual review of measured failure."},
                    runner.observation(healthy=False))
                assert manual["automation_eligible"] is False
                assert manual["blocked_reason"] == "target_reconciliation_required"
                auto_spec = make_spec(tmp_path, compose=True, automatic=[action])
                # Keep identity stable for the already-captured snapshot path.
                auto_spec = auto_spec.model_copy(update={
                    "compose_file": spec.compose_file,
                    "source_dir": spec.source_dir, "source_revision": spec.source_revision,
                    "deployment_launch": spec.deployment_launch})
                fresh.put_target(auto_spec.model_dump(mode="json"))
                automatic = executor.create_proposal(auto_spec, "inv-auto", {
                    "action": action.value, "reason": "Automatic policy evaluation."},
                    runner.observation(healthy=False))
                assert automatic["automation_eligible"] is False
                assert automatic["blocked_reason"] == "target_reconciliation_required"
                claimed = claim(fresh, manual)
                await expect_error("target_reconciliation_required",
                                   executor.execute(claimed, spec, asyncio.Event()))
                assert not runner.mutations
            finally:
                fresh.close()
    asyncio.run(scenario())


def test_unknown_blocks_deploy_build_and_execute_direct_calls(store, tmp_path):
    async def scenario():
        from test_ops_service import target as svc_target
        spec = svc_target("alpha")
        store.put_target(spec.model_dump(mode="json"))
        put_unknown(store, "alpha")
        runner_calls = []
        async def dead_runner(argv, **limits):
            runner_calls.append(argv)
            raise AssertionError("Blocked deployment must not touch Docker")
        manager = DeploymentManager(store, runner=dead_runner)
        with pytest.raises(OpsError) as build_err:
            await manager.build_proposal(spec, "inv-1", {}, observed())
        assert build_err.value.code == "target_reconciliation_required"
        with pytest.raises(OpsError) as exec_err:
            await manager.execute({"proposal_id": "x", "target_id": "alpha"}, spec, asyncio.Event())
        assert exec_err.value.code == "target_reconciliation_required"
        with pytest.raises(OpsError) as rollback_err:
            await manager.rollback({"proposal_id": "x", "target_id": "alpha"}, spec, asyncio.Event())
        assert rollback_err.value.code == "target_reconciliation_required"
        assert runner_calls == []
    asyncio.run(scenario())


def test_persisted_unknown_survives_new_store_instance(tmp_path):
    root = tmp_path / "ops"
    first = OpsStore(root)
    first.put_target(make_spec(tmp_path).model_dump(mode="json"))
    put_unknown(first, "bookmarks")
    first.close()
    second = OpsStore(root)
    try:
        assert len(second.list_unresolved_unknowns("bookmarks")) == 1
        async def scenario():
            spec, runner = make_spec(tmp_path), FakeRunner()
            executor = ActionExecutor(second, runner=runner)
            proposal = executor.create_proposal(spec, "inv-new", {
                "action": "restart_container", "reason": "After restart."},
                runner.observation(healthy=False))
            assert proposal["blocked_reason"] == "target_reconciliation_required"
            await expect_error("target_reconciliation_required",
                               executor.execute(claim(second, proposal), spec, asyncio.Event()))
        asyncio.run(scenario())
    finally:
        second.close()


def test_older_unknown_behind_many_investigations_still_blocks(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        store.put_target(spec.model_dump(mode="json"))
        old_inv = store.create_investigation("bookmarks", "Old", trigger="manual")
        store.put_proposal({"proposal_id": "very-old-unknown", "target_id": "bookmarks",
                            "investigation_id": old_inv["investigation_id"],
                            "status": "unknown", "action": "restart_container"})
        for i in range(60):
            store.create_investigation("bookmarks", f"Recent {i}", trigger="manual")
        assert len(store.list_investigations("bookmarks", limit=50)) == 50
        assert len(store.list_unresolved_unknowns("bookmarks")) == 1
        proposal = executor.create_proposal(spec, "inv-fresh", {
            "action": "restart_container", "reason": "Fresh manual review."},
            runner.observation(healthy=False))
        assert proposal["blocked_reason"] == "target_reconciliation_required"
    asyncio.run(scenario())


def test_unknown_during_final_preflight_prevents_mutation(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        configs = 0
        original = executor.runner
        async def tampering(argv, **limits):
            nonlocal configs
            if argv[3] == "compose" and "config" in argv:
                configs += 1
                if configs == 2:
                    await asyncio.sleep(0)
                    put_unknown(store, "bookmarks", "racing-unknown")
            return await original(argv, **limits)
        executor.runner = tampering
        await expect_error("target_reconciliation_required",
                           executor.execute(proposal, spec, asyncio.Event()))
        assert not runner.mutations
        assert configs == 2
    asyncio.run(scenario())


def test_unknown_during_deployment_preflight_prevents_mutation(store, tmp_path):
    async def scenario():
        from test_ops_deployment import setup, proposal as build, approve
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await build(spec, runner, patch, manager))
        original_snapshot = manager._snapshot
        checks = 0
        async def tampering(current_spec, snapshot_hash):
            nonlocal checks
            result = await original_snapshot(current_spec, snapshot_hash)
            checks += 1
            if checks == 2:
                await asyncio.sleep(0)
                put_unknown(store, spec.target_id, "racing-deploy-unknown")
            return result
        manager._snapshot = tampering
        await expect_error("target_reconciliation_required",
                           manager.execute(value, spec, asyncio.Event()))
        assert not runner.app_mutations
        assert checks == 2
    asyncio.run(scenario())


def test_no_paid_automatic_diagnosis_when_blocked(tmp_path):
    async def run():
        investigator = Investigator()
        service = make_service(tmp_path, investigator=investigator)
        try:
            await service.enroll(target())
            await service.set_policy("alpha", __import__("aura_ops.models", fromlist=["Policy"]).Policy(auto_investigate=True))
            service.store.put_proposal({"proposal_id": "old-unknown", "target_id": "alpha",
                                        "status": "unknown", "action": "restart_container"})
            with pytest.raises(OpsError) as err:
                await service.start_investigation("alpha", "Auto", trigger="monitor")
            assert err.value.code == "target_reconciliation_required"
            assert investigator.calls == []
            assert service.store.list_investigations("alpha") == []
            # Manual read-only investigation is allowed but its action is blocked.
            row = await service.start_investigation("alpha", "Manual review")
            await until(lambda: service.investigation(row["investigation_id"])["phase"] == "needs_human")
            # Abstaining fake diagnosis ends without proposal; gate is enforced at proposal/execute.
            assert investigator.calls == ["alpha"]
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_manual_action_visibly_blocked_with_reconciliation_code(tmp_path):
    async def run():
        class Acting(Investigator):
            async def diagnose(self, spec, evidence, cancel, emit, refresh=None):
                self.calls.append(spec.target_id)
                return {"diagnosis": {"summary": "Measured failure", "action": "restart_container",
                    "reason": "Temporary failure in observed version", "needs_source_analysis": False,
                    "findings": [{"hypothesis": "Restart helps", "confidence": "supported",
                                  "evidence_refs": ["observation.current"]}]},
                        "evidence": evidence, "calls": []}
        docker = Docker(observed(healthy=False))
        service = make_service(tmp_path, investigator=Acting(), docker_factory=docker)
        try:
            await service.enroll(target())
            service.store.put_proposal({"proposal_id": "old-unknown", "target_id": "alpha",
                                        "status": "unknown", "action": "restart_container"})
            row = await service.start_investigation("alpha", "Manual review")
            await until(lambda: service.investigation(row["investigation_id"])["phase"] == "needs_human")
            final = service.investigation(row["investigation_id"])
            assert final["error"]["code"] == "target_reconciliation_required"
            proposals = service.store.list_proposals(row["investigation_id"])
            assert proposals and proposals[0]["blocked_reason"] == "target_reconciliation_required"
            assert proposals[0]["status"] == "blocked"
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_unrelated_target_can_still_proceed(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        other = make_spec(tmp_path)
        other = other.model_copy(update={"target_id": "other", "name": "Other",
                                          "container_name": "other"})
        store.put_target(other.model_dump(mode="json"))
        put_unknown(store, "bookmarks")
        blocked = executor.create_proposal(spec, "inv-blocked", {
            "action": "restart_container", "reason": "Blocked target."},
            runner.observation(healthy=False))
        assert blocked["blocked_reason"] == "target_reconciliation_required"
        assert len(store.list_unresolved_unknowns("bookmarks")) == 1
        assert store.list_unresolved_unknowns("other") == []
        ok = executor.create_proposal(other, "inv-other", {
            "action": "restart_container", "reason": "Unrelated target intervention."},
            runner.observation(healthy=False))
        assert ok.get("blocked_reason") is None
        result = await executor.execute(claim(store, ok), other, asyncio.Event())
        assert result["command_completed"] is True
        assert len(runner.mutations) == 1
    asyncio.run(scenario())


def test_pre_dispatch_cancel_does_not_invent_unknown(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            spec = target()
            await service.enroll(spec)
            row = service.store.create_investigation("alpha", "Investigate")
            proposal = service.executor.create_proposal(spec, row["investigation_id"],
                {"action": "restart_container", "reason": "Known temporary outage"}, observed())
            service.store.update_investigation(row["investigation_id"], phase="awaiting_approval")
            cancel = asyncio.Event()
            cancel.set()
            await service._act(row, proposal, cancel)
            final = service.store.get_proposal(proposal["proposal_id"])
            assert final["status"] == "cancelled"
            assert service.store.list_unresolved_unknowns("alpha") == []
            assert service.investigation(row["investigation_id"])["phase"] == "cancelled"
            # No gate: a fresh unimpaired proposal is not reconciliation-blocked.
            fresh = service.executor.create_proposal(spec, "inv-fresh", {
                "action": "restart_container", "reason": "Fresh review."}, observed())
            assert fresh.get("blocked_reason") is None
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_post_dispatch_cancel_remains_unknown(tmp_path):
    async def run():
        cancel = asyncio.Event()
        class DispatchThenCancel:
            async def execute(self, proposal, spec, cancel_event):
                cancel_event.set()
                return {"identity": observed()["identity"]}
        service = make_service(tmp_path, executor=DispatchThenCancel())
        try:
            spec = target()
            await service.enroll(spec)
            row = service.store.create_investigation("alpha", "Investigate")
            proposal = service.store.put_proposal({"target_id": "alpha",
                "investigation_id": row["investigation_id"], "action": "restart_container",
                "proposal_hash": "a" * 64})
            await service._act(row, proposal, cancel)
            final = service.store.get_proposal(proposal["proposal_id"])
            assert final["status"] == "unknown"
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
            assert service.investigation(row["investigation_id"])["error"]["code"] == "action_outcome_unknown"
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == "unknown"
            assert service.store.list_unresolved_unknowns("alpha")
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_ambiguous_legacy_cancel_is_conservatively_unknown(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Investigate")
            # Legacy ambiguous record: executing without authorization evidence.
            proposal = service.store.put_proposal({"target_id": "alpha",
                "investigation_id": row["investigation_id"], "action": "restart_container",
                "status": "executing"})
            service.store.update_investigation(row["investigation_id"], phase="executing",
                                               proposal_id=proposal["proposal_id"])
            await service._record_cancel(row)
            assert service.investigation(row["investigation_id"])["phase"] == "needs_human"
            assert service.investigation(row["investigation_id"])["error"]["code"] == "action_outcome_unknown"
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == "unknown"
            assert service.store.list_unresolved_unknowns("alpha")
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_verified_preserved_when_promotion_cancelled(tmp_path):
    async def run():
        import aura_ops.promotion as promotion_module
        from test_ops_deployment_flow import Deployment
        docker = Docker()
        deployment = Deployment(docker)
        async def functional(spec, cancel, **kwargs):
            return {"verified": True, "reason": "passed", "identity": {
                k: v for k, v in observed(image="c")["identity"].items() if k != "image_ref"}}
        service = make_service(tmp_path, docker_factory=docker, deployment_manager=deployment,
                               functional_verifier=functional)
        async def cancelled_promote(*args, **kwargs):
            raise asyncio.CancelledError()
        original = promotion_module.promote
        promotion_module.promote = cancelled_promote
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Fix")
            service.store.update_investigation(row["investigation_id"], phase="awaiting_approval")
            proposal = service.store.put_proposal({"target_id": "alpha",
                "investigation_id": row["investigation_id"], "action": "deploy_verified_patch",
                "proposal_hash": "a" * 64, "rollback_on_failure": True})
            await service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
            await until(lambda: service._task and service._task.done())
            final_proposal = service.store.get_proposal(proposal["proposal_id"])
            assert final_proposal["status"] == "verified"
            final_inv = service.investigation(row["investigation_id"])
            assert final_inv["phase"] == "verified_fixed"
            assert final_inv["baseline_error"]["code"] == "promotion_interrupted"
            assert service.store.list_unresolved_unknowns("alpha") == []
        finally:
            promotion_module.promote = original
            await service.shutdown()
    asyncio.run(run())


def test_baseline_reconnect_and_time_do_not_clear_unknown(tmp_path):
    async def run():
        from aura_ops.models import Policy
        service = make_service(tmp_path)
        try:
            spec = target()
            await service.enroll(spec)
            await service.set_policy("alpha", Policy(auto_investigate=True))
            service.store.put_proposal({"proposal_id": "old-unknown", "target_id": "alpha",
                                        "status": "unknown", "action": "restart_container"})
            await service.capture_snapshot("alpha")
            await service.disconnect("alpha")
            await service.reconnect("alpha")
            service.store.put_setting("lastrestart.alpha", time.time() - 7200)
            with pytest.raises(OpsError) as err:
                await service.start_investigation("alpha", "Auto", trigger="monitor")
            assert err.value.code == "target_reconciliation_required"
            row = service.store.create_investigation("alpha", "Manual")
            proposal = service.executor.create_proposal(spec, row["investigation_id"],
                {"action": "restart_container", "reason": "Manual after reconnect."}, observed())
            assert proposal["blocked_reason"] == "target_reconciliation_required"
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
            # Read APIs remain available.
            assert service.snapshot()["seq"] >= 0
            assert service.list_targets()
            assert service.investigation(row["investigation_id"])
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_normal_flow_and_one_shot_rollback_still_work(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        proposal = claim(store, create(executor, spec, runner))
        result = await executor.execute(proposal, spec, asyncio.Event())
        assert result["command_completed"] is True
        assert store.list_unresolved_unknowns("bookmarks") == []
    asyncio.run(scenario())
    async def rollback_check():
        from test_ops_deployment import setup, proposal as build, approve
        spec, runner, patch, manager = await setup(tmp_path, store)
        value = approve(store, await build(spec, runner, patch, manager))
        await manager.execute(value, spec, asyncio.Event())
        store.update_proposal(value["proposal_id"], status="verification_failed")
        result = await manager.rollback(value, spec, asyncio.Event())
        assert result["command_completed"] is True
        assert store.list_unresolved_unknowns(spec.target_id) == []
    asyncio.run(rollback_check())


def test_rejected_retry_cannot_erase_its_own_unknown(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Interrupted action")
            proposal = service.store.put_proposal({"target_id": "alpha",
                "investigation_id": row["investigation_id"], "action": "restart_container",
                "status": "unknown", "proposal_hash": "a" * 64})
            await service._act(row, proposal, asyncio.Event())
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == "unknown"
            assert service.store.list_unresolved_unknowns("alpha")
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("during_collection", [False, True])
def test_queued_monitor_rechecks_gate_before_paid_diagnosis(tmp_path, during_collection):
    async def run():
        from aura_ops.models import Policy
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            await service.set_policy("alpha", Policy(auto_investigate=True))
            row = service.store.create_investigation("alpha", "Queued monitor", trigger="monitor")
            if during_collection:
                async def collect(spec):
                    await asyncio.sleep(0)
                    put_unknown(service.store, "alpha")
                    return observed()
                service._observe = collect
            else:
                put_unknown(service.store, "alpha")
            await service._investigate(row, asyncio.Event())
            assert service.investigator.calls == []
            assert service.investigation(row["investigation_id"])["error"]["code"] == "target_reconciliation_required"
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_verifier_exception_after_dispatch_keeps_target_blocked(tmp_path):
    async def run():
        class Completed:
            async def execute(self, proposal, spec, cancel):
                return {"command_completed": True, "identity": observed()["identity"]}
        service = make_service(tmp_path, executor=Completed())
        async def broken_verify(*args, **kwargs):
            raise RuntimeError("Verifier interrupted before obtaining a result")
        service.verify = broken_verify
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Action")
            proposal = service.store.put_proposal({"target_id": "alpha",
                "investigation_id": row["investigation_id"], "action": "restart_container",
                "proposal_hash": "a" * 64})
            await service._act(row, proposal, asyncio.Event())
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == "unknown"
            assert service.store.list_unresolved_unknowns("alpha")
        finally:
            await service.shutdown()
    asyncio.run(run())
