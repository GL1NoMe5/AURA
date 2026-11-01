"""OPS-LIFECYCLE-GUARD-001: phase/baseline protection and safe restart recovery (offline)."""
from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aura_ops.models import OpsError, PatchProfile
from aura_ops.store import OpsStore
from test_ops_service import Docker, Investigator, make_service, observed, target, until


REVISION = "d" * 40
OTHER_REVISION = "e" * 40


def source_target(tmp_path, name="alpha"):
    import hashlib
    import json
    parent = Path(tmp_path) / f"enroll-{name}"
    source = parent / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "app.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
    tests_dir = source / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_app.py").write_text("def test_existing():\n    assert True\n",
                                           encoding="utf-8")
    files = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in source.rglob("*") if p.is_file()}
    (parent / "source-manifest.json").write_text(json.dumps(
        {"source_dir": str(source), "revision": REVISION, "files": files}), encoding="utf-8")
    spec = target(name)
    spec.source_dir = str(source)
    spec.source_revision = REVISION
    spec.patch_profile = PatchProfile(
        image="sha256:" + "f" * 64,
        test_paths=["tests/test_app.py"],
    )
    return spec


def diagnosis_evidence(identity=None):
    ident = identity or observed()["identity"]
    return {
        "observation.current": {**observed(), "identity": deepcopy(ident)},
        "target.policy": {"automatic_actions": []},
    }


def make_diagnosis_row(store, target_id, phase="needs_human", identity=None):
    row = store.create_investigation(target_id, "Investigate measured fault")
    store.update_investigation(
        row["investigation_id"],
        phase=phase,
        diagnosis={"summary": "Measured fault", "action": "abstain",
                   "reason": "Needs source fix", "needs_source_analysis": True,
                   "findings": []},
        evidence=diagnosis_evidence(identity),
        source_revision=REVISION,
    )
    return store.get_investigation(row["investigation_id"])


class FakePatchEngine:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "needs_human", "reason": "fake-no-fix"}

    async def prepare(self, spec, investigation, cancel, emit):
        self.calls.append(investigation["investigation_id"])
        return deepcopy(self.result)


def test_prepare_patch_never_overwrites_verified_or_tested(tmp_path):
    async def run():
        service = make_service(tmp_path, patch_engine=FakePatchEngine())
        try:
            await service.enroll(source_target(tmp_path))
            # Verified correction with pending promotion retry.
            verified = make_diagnosis_row(service.store, "alpha", phase="verified_fixed")
            patch = {"patch_id": "a" * 32, "target_id": "alpha",
                     "investigation_id": verified["investigation_id"],
                     "status": "tests_passed", "source_revision": REVISION}
            proposal = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": verified["investigation_id"],
                "action": "deploy_verified_patch", "status": "verified",
                "patch_id": patch["patch_id"],
                "verification": {"verified": True, "root_cause_fixed": True,
                                 "functional": {"verified": True}}})
            verification = {"verified": True, "kind": "tested_patch_and_runtime",
                            "root_cause_fixed": True}
            baseline_error = {"code": "promotion_failed",
                              "message": "Baseline bookkeeping failed; retry promotion."}
            service.store.update_investigation(
                verified["investigation_id"], patch=patch,
                proposal_id=proposal["proposal_id"], verification=verification,
                baseline_error=baseline_error)
            before = deepcopy(service.investigation(verified["investigation_id"]))
            before_proposal = deepcopy(service.store.get_proposal(proposal["proposal_id"]))
            with pytest.raises(OpsError):
                await service.prepare_patch(verified["investigation_id"])
            after = service.investigation(verified["investigation_id"])
            assert after["phase"] == "verified_fixed"
            assert after["patch"] == before["patch"]
            assert after["proposal_id"] == before["proposal_id"]
            assert after["verification"] == before["verification"]
            assert after["baseline_error"] == before["baseline_error"]
            assert service.store.get_proposal(proposal["proposal_id"]) == before_proposal
            assert service.patch_engine.calls == []

            # Existing tested fix must use prepare-deployment, not overwrite.
            fixed = make_diagnosis_row(service.store, "alpha", phase="fix_proposed")
            tested = {"patch_id": "b" * 32, "target_id": "alpha",
                      "investigation_id": fixed["investigation_id"],
                      "status": "tests_passed", "source_revision": REVISION}
            service.store.update_investigation(fixed["investigation_id"], patch=tested)
            with pytest.raises(OpsError):
                await service.prepare_patch(fixed["investigation_id"])
            assert service.investigation(fixed["investigation_id"])["patch"] == tested
            assert service.patch_engine.calls == []

            # Active work and awaiting approval are never overwritten.
            for phase in ("queued", "investigating", "awaiting_approval", "executing",
                          "verifying", "preparing_patch", "preparing_deployment"):
                active = make_diagnosis_row(service.store, "alpha", phase=phase)
                with pytest.raises(OpsError):
                    await service.prepare_patch(active["investigation_id"])
                assert service.investigation(active["investigation_id"])["phase"] == phase
            assert service.patch_engine.calls == []
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_promotion_retry_survives_rejected_prepare_and_new_check(tmp_path, monkeypatch):
    async def run():
        service = make_service(tmp_path, patch_engine=FakePatchEngine())
        try:
            await service.enroll(source_target(tmp_path))
            verified = make_diagnosis_row(service.store, "alpha", phase="verified_fixed")
            patch = {"patch_id": "c" * 32, "target_id": "alpha",
                     "investigation_id": verified["investigation_id"],
                     "status": "tests_passed", "source_revision": REVISION}
            proposal = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": verified["investigation_id"],
                "action": "deploy_verified_patch", "status": "verified",
                "patch_id": patch["patch_id"],
                "verification": {"verified": True, "root_cause_fixed": True,
                                 "functional": {"verified": True}}})
            service.store.update_investigation(
                verified["investigation_id"], patch=patch,
                proposal_id=proposal["proposal_id"],
                verification={"verified": True, "root_cause_fixed": True},
                baseline_error={"code": "promotion_failed", "message": "Retry me."})
            with pytest.raises(OpsError):
                await service.prepare_patch(verified["investigation_id"])
            # Unrelated new check creates a new record and leaves history intact.
            fresh = await service.start_investigation("alpha", "New unrelated check")
            assert fresh["investigation_id"] != verified["investigation_id"]
            historic = service.investigation(verified["investigation_id"])
            assert historic["phase"] == "verified_fixed"
            assert historic["patch"] == patch
            assert historic["proposal_id"] == proposal["proposal_id"]
            assert historic["baseline_error"]["code"] == "promotion_failed"
            # Cancel the unrelated check so promotion retry is not busy.
            await service.cancel(fresh["investigation_id"])

            import aura_ops.promotion as promotion_module
            calls = []

            async def fake_promote(manager, spec, proposal_id):
                calls.append(proposal_id)
                assert proposal_id == proposal["proposal_id"]
                return {"source_manifest_sha256": "d" * 64,
                        "identity": observed()["identity"],
                        "container_mutated": False}

            monkeypatch.setattr(promotion_module, "promote", fake_promote)
            result = await service.promote_deployment(proposal["proposal_id"])
            assert calls == [proposal["proposal_id"]]
            assert result["container_mutated"] is False
            retried = service.investigation(verified["investigation_id"])
            assert retried["phase"] == "verified_fixed"
            assert retried["baseline_error"] is None
            assert retried["baseline_promoted"] is True
            assert service.patch_engine.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["source", "identity"])
def test_stale_source_or_identity_fails_before_provider(tmp_path, fault):
    async def run():
        docker = Docker(observed())
        engine = FakePatchEngine()
        service = make_service(tmp_path, docker_factory=docker, patch_engine=engine)
        try:
            await service.enroll(source_target(tmp_path))
            row = make_diagnosis_row(service.store, "alpha", phase="needs_human",
                                     identity=observed()["identity"])
            if fault == "source":
                current = service.store.get_target("alpha")
                current["source_revision"] = OTHER_REVISION
                service.store.put_target(current)
            else:
                docker.state = observed(image="c")
            with pytest.raises(OpsError) as err:
                await service.prepare_patch(row["investigation_id"])
            assert err.value.code in ("source_changed", "target_changed", "diagnosis_obsolete")
            assert engine.calls == []
            assert service.investigator.calls == []
            # Failed entry preserves existing patch/proposal state (none yet).
            assert service.investigation(row["investigation_id"])["phase"] == "needs_human"
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_failed_preparation_retry_starts_only_with_valid_binding(tmp_path):
    async def run():
        engine = FakePatchEngine(result={"status": "needs_human", "reason": "still-failing"})
        service = make_service(tmp_path, patch_engine=engine)
        try:
            await service.enroll(source_target(tmp_path))
            row = make_diagnosis_row(service.store, "alpha", phase="needs_human",
                                     identity=observed()["identity"])
            failed_patch = {"patch_id": "d" * 32, "target_id": "alpha",
                            "investigation_id": row["investigation_id"],
                            "status": "needs_human", "source_revision": REVISION}
            service.store.update_investigation(row["investigation_id"], patch=failed_patch)
            started = await service.prepare_patch(row["investigation_id"])
            assert started["phase"] == "preparing_patch"
            await until(lambda: service._task is not None and service._task.done())
            assert engine.calls == [row["investigation_id"]]
            final = service.investigation(row["investigation_id"])
            assert final["phase"] == "needs_human"
            assert final["patch"]["status"] == "needs_human"
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_capture_rejects_pending_approval_beyond_50_and_preserves_baseline(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            await service.enroll(target("beta"))
            old_snapshot = {"snapshot_id": "baseline-1", "target_id": "alpha",
                            "identity": observed()["identity"], "marker": "keep-me"}
            service.store.put_setting("snapshot.alpha", deepcopy(old_snapshot))
            # Old pending approval pushed outside the 50-row UI window.
            pending = service.store.create_investigation("alpha", "Old pending work")
            proposal = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": pending["investigation_id"],
                "action": "restart_container", "status": "proposed",
                "proposal_hash": "a" * 64})
            service.store.update_investigation(pending["investigation_id"],
                                               phase="awaiting_approval",
                                               proposal_id=proposal["proposal_id"])
            for index in range(60):
                service.store.create_investigation("alpha", f"Recent {index}")
            assert len(service.store.list_investigations("alpha", limit=50)) == 50
            with pytest.raises(OpsError) as err:
                await service.capture_snapshot("alpha")
            assert err.value.code == "operation_busy"
            assert service.store.get_setting("snapshot.alpha") == old_snapshot
            live = service.store.get_proposal(proposal["proposal_id"])
            assert live["status"] == "proposed"
            assert live["proposal_hash"] == "a" * 64
            # Unrelated idle target is not permanently blocked.
            beta_result = await service.capture_snapshot("beta")
            assert beta_result["target_id"] == "beta"
            assert service.store.get_setting("snapshot.alpha") == old_snapshot
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_capture_rejects_when_state_changes_during_preflight(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            old_snapshot = {"snapshot_id": "baseline-2", "target_id": "alpha",
                            "identity": observed()["identity"]}
            service.store.put_setting("snapshot.alpha", deepcopy(old_snapshot))
            original = service.executor.capture_snapshot

            async def racing_capture(spec, observation, persist=True):
                assert persist is False
                await asyncio.sleep(0)
                intruder = service.store.create_investigation("alpha", "Concurrent approval")
                service.store.update_investigation(intruder["investigation_id"],
                                                   phase="awaiting_approval")
                return await original(spec, observation, persist=False)

            service.executor.capture_snapshot = racing_capture
            with pytest.raises(OpsError) as err:
                await service.capture_snapshot("alpha")
            assert err.value.code == "operation_busy"
            assert service.store.get_setting("snapshot.alpha") == old_snapshot
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_prepare_patch_blocked_by_awaiting_approval_beyond_50(tmp_path):
    async def run():
        service = make_service(tmp_path, patch_engine=FakePatchEngine())
        try:
            await service.enroll(source_target(tmp_path))
            old = service.store.create_investigation("alpha", "Old approval")
            service.store.update_investigation(old["investigation_id"], phase="awaiting_approval")
            for index in range(60):
                service.store.create_investigation("alpha", f"Filler {index}")
            row = make_diagnosis_row(service.store, "alpha", phase="needs_human",
                                     identity=observed()["identity"])
            with pytest.raises(OpsError) as err:
                await service.prepare_patch(row["investigation_id"])
            assert err.value.code == "operation_busy"
            assert service.patch_engine.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_startup_returns_preparing_deployment_to_fix_proposed(tmp_path, monkeypatch):
    async def unavailable():
        from aura_ops.models import OpsError as _Err
        raise _Err("docker_unavailable", "Engine not available", 503)
    import aura_ops.service as service_module
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(source_target(tmp_path))
            row = make_diagnosis_row(service.store, "alpha", phase="preparing_deployment",
                                     identity=observed()["identity"])
            patch = {"patch_id": "e" * 32, "target_id": "alpha",
                     "investigation_id": row["investigation_id"],
                     "status": "tests_passed", "source_revision": REVISION,
                     "completed_at": "2026-10-29T00:00:00Z"}
            service.store.update_investigation(row["investigation_id"], patch=patch)
            await service.startup()
            await service.shutdown()
            # Reopen to prove durability across instances.
            reopened = OpsStore(service.store.root)
            try:
                resumed = reopened.get_investigation(row["investigation_id"])
                assert resumed["phase"] == "fix_proposed"
                assert resumed["patch"] == patch
                assert "freshly" not in (resumed.get("error") or {}).get("message", "").lower()
                assert resumed["patch"]["completed_at"] == "2026-10-29T00:00:00Z"
            finally:
                reopened.close()
            assert service.investigator.calls == []
            assert service._task is None
        finally:
            try:
                await service.shutdown()
            except Exception:
                pass
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["target", "investigation", "status", "source",
                                   "dispatch", "unknown"])
def test_startup_rejects_inconsistent_or_dispatched_rebuild(tmp_path, monkeypatch, fault):
    async def unavailable():
        from aura_ops.models import OpsError as _Err
        raise _Err("docker_unavailable", "Engine not available", 503)
    import aura_ops.service as service_module
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(source_target(tmp_path))
            row = make_diagnosis_row(service.store, "alpha", phase="preparing_deployment",
                                     identity=observed()["identity"])
            patch = {"patch_id": "f" * 32, "target_id": "alpha",
                     "investigation_id": row["investigation_id"],
                     "status": "tests_passed", "source_revision": REVISION}
            if fault == "target":
                patch["target_id"] = "beta"
            elif fault == "investigation":
                patch["investigation_id"] = "0" * 32
            elif fault == "status":
                patch["status"] = "needs_human"
            elif fault == "source":
                patch["source_revision"] = OTHER_REVISION
            service.store.update_investigation(row["investigation_id"], patch=patch)
            if fault == "dispatch":
                service.store.put_proposal({
                    "target_id": "alpha", "investigation_id": row["investigation_id"],
                    "action": "deploy_verified_patch", "status": "dispatching",
                    "patch_id": patch["patch_id"],
                    "dispatch_attempted_at": "2026-10-29T00:00:00Z"})
            if fault == "unknown":
                service.store.put_proposal({
                    "proposal_id": f"unknown-{fault}", "target_id": "alpha",
                    "status": "unknown", "action": "restart_container"})
            await service.startup()
            resumed = service.investigation(row["investigation_id"])
            assert resumed["phase"] == "needs_human"
            assert resumed["patch"] == patch
            assert resumed["error"] is not None
            assert service.investigator.calls == []
            assert service._task is None
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_restart_expires_old_proposal_and_requires_fresh_approval(tmp_path, monkeypatch):
    async def unavailable():
        from aura_ops.models import OpsError as _Err
        raise _Err("docker_unavailable", "Engine not available", 503)
    import aura_ops.service as service_module
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(source_target(tmp_path))
            row = make_diagnosis_row(service.store, "alpha", phase="awaiting_approval",
                                     identity=observed()["identity"])
            patch = {"patch_id": "a1" * 16, "target_id": "alpha",
                     "investigation_id": row["investigation_id"],
                     "status": "tests_passed", "source_revision": REVISION}
            old = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": row["investigation_id"],
                "action": "deploy_verified_patch", "status": "proposed",
                "patch_id": patch["patch_id"], "proposal_hash": "b" * 64})
            service.store.update_investigation(row["investigation_id"], patch=patch,
                                               proposal_id=old["proposal_id"])
            await service.startup()
            assert service.investigation(row["investigation_id"])["phase"] == "fix_proposed"
            assert service.store.get_proposal(old["proposal_id"])["status"] == "expired"
            with pytest.raises(OpsError):
                await service.decide(old["proposal_id"], "b" * 64, True)

            built = {"proposal_id": "fresh-proposal", "target_id": "alpha",
                     "investigation_id": row["investigation_id"],
                     "action": "deploy_verified_patch", "status": "proposed",
                     "patch_id": patch["patch_id"], "proposal_hash": "c" * 64}

            async def fake_build(spec, investigation_id, patch_arg, observation):
                assert investigation_id == row["investigation_id"]
                return service.store.put_proposal(deepcopy(built))

            service.deployment_manager.build_proposal = fake_build
            # Recovery checks are required for deployment preparation.
            current = service.store.get_target("alpha")
            current["recovery_checks"] = [{
                "url": "http://127.0.0.1:18090/health", "expected_status": 200,
                "expected_sha256": "d" * 64, "expected_bytes": 10}]
            service.store.put_target(current)
            await service.prepare_deployment(row["investigation_id"])
            await until(lambda: service._task is not None and service._task.done())
            pending = service.investigation(row["investigation_id"])
            assert pending["phase"] == "awaiting_approval"
            fresh = service.store.get_proposal("fresh-proposal")
            assert fresh["proposal_hash"] == "c" * 64
            assert fresh["proposal_hash"] != old["proposal_hash"]
            assert fresh.get("authorization") is None
            assert service.store.get_proposal(old["proposal_id"])["status"] == "expired"
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_interrupted_verification_with_executed_status_creates_gate(tmp_path, monkeypatch):
    async def unavailable():
        from aura_ops.models import OpsError as _Err
        raise _Err("docker_unavailable", "Engine not available", 503)
    import aura_ops.service as service_module
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Recovery action")
            service.store.update_investigation(row["investigation_id"], phase="verifying")
            proposal = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": row["investigation_id"],
                "action": "restart_container", "status": "executed",
                "result": {"command_completed": True,
                           "identity": observed()["identity"]}})
            await service.startup()
            assert service.investigation(row["investigation_id"])["phase"] == "needs_human"
            gated = service.store.get_proposal(proposal["proposal_id"])
            assert gated["status"] == "unknown"
            assert gated["result"]["command_completed"] is True
            unknowns = service.store.list_unresolved_unknowns("alpha")
            assert [item["proposal_id"] for item in unknowns] == [proposal["proposal_id"]]
            from aura_ops.actions import _check_reconciliation
            with pytest.raises(OpsError) as err:
                _check_reconciliation(service.store, "alpha")
            assert err.value.code == "target_reconciliation_required"
            # Idempotent: second restart keeps the same gate, no new records.
            await service.startup()
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == "unknown"
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_completed_recovery_and_fix_survive_repeated_startups(tmp_path, monkeypatch):
    async def unavailable():
        from aura_ops.models import OpsError as _Err
        raise _Err("docker_unavailable", "Engine not available", 503)
    import aura_ops.service as service_module
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(source_target(tmp_path))
            mitigated = service.store.create_investigation("alpha", "Mitigated")
            verification = {"verified": True, "kind": "availability"}
            recovered = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": mitigated["investigation_id"],
                "action": "restart_container", "status": "executed",
                "result": {"command_completed": True},
                "verification": deepcopy(verification)})
            service.store.update_investigation(mitigated["investigation_id"],
                                               phase="mitigated",
                                               verification=deepcopy(verification),
                                               proposal_id=recovered["proposal_id"])
            fixed = make_diagnosis_row(service.store, "alpha", phase="verified_fixed")
            patch = {"patch_id": "b1" * 16, "status": "tests_passed"}
            deployed = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": fixed["investigation_id"],
                "action": "deploy_verified_patch", "status": "verified",
                "patch_id": patch["patch_id"],
                "verification": {"verified": True, "root_cause_fixed": True,
                                 "functional": {"verified": True}}})
            service.store.update_investigation(
                fixed["investigation_id"], patch=patch,
                proposal_id=deployed["proposal_id"],
                verification={"verified": True, "root_cause_fixed": True},
                baseline_error={"code": "promotion_failed", "message": "Retry later."})
            await service.startup()
            await service.startup()
            assert service.investigation(mitigated["investigation_id"])["phase"] == "mitigated"
            assert service.store.get_proposal(recovered["proposal_id"])["status"] == "executed"
            assert service.store.get_proposal(recovered["proposal_id"])["verification"] == verification
            survived = service.investigation(fixed["investigation_id"])
            assert survived["phase"] == "verified_fixed"
            assert survived["baseline_error"]["code"] == "promotion_failed"
            assert service.store.get_proposal(deployed["proposal_id"])["status"] == "verified"
            assert service.store.list_unresolved_unknowns("alpha") == []
            assert service.investigator.calls == []
            assert service._task is None
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_scan_helper_covers_history_beyond_1000(tmp_path):
    store = OpsStore(tmp_path / "store")
    try:
        store.put_target(target().model_dump(mode="json"))
        for index in range(1050):
            store.create_investigation("alpha", f"History {index}")
        assert len(store.list_investigations(limit=1000)) == 1000
        assert len(store.scan_investigations("alpha")) == 1050
        assert len(store.scan_investigations()) == 1050
    finally:
        store.close()
