"""OPS-TARGETS-001: connection editing/removal without losing history."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aura_ops.service as service_module
from aura_ops.actions import ActionExecutor
from aura_ops.models import OpsError, Policy, TargetSpec, utc_now
from aura_ops.service import OpsService
from aura_ops.store import OpsStore
from test_ops_service import Docker, Investigator, make_service, observed, target, until


def spec(tid="alpha", **overrides):
    data = target(tid).model_dump(mode="json")
    data["monitoring"] = False
    data.update(overrides)
    return TargetSpec(**data)


def err_code(excinfo):
    return excinfo.value.code


async def enroll_disconnected(service, item):
    await service.enroll(item)
    await service.disconnect(item.target_id)
    return service.target_view(item.target_id)


def make_source(tmp_path, dirname="src-one"):
    root = tmp_path / dirname
    root.mkdir()
    content = b"VALUE = 1\n"
    (root / "app.py").write_bytes(content)
    revision = "a" * 40
    manifest = {"source_dir": str(root), "revision": revision,
                "files": {"app.py": hashlib.sha256(content).hexdigest()}}
    manifest_path = tmp_path / "source-manifest.json"
    # SourceIndex looks for manifest next to the source root; each test
    # using a source builds it in its own tmp_path, so one file suffices.
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    expected = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return root, revision, expected


# 1 ---------------------------------------------------------------------
def test_remove_frees_slot_and_other_records_unchanged(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            for name in ("alpha", "beta", "gamma"):
                await service.enroll(target(name))
            before_beta = service.store.get_target("beta")
            await service.disconnect("alpha")
            result = await service.remove_target("alpha", 0)
            assert result == {"target_id": "alpha", "removed": True,
                              "history_retained": True, "container_stopped": False}
            await service.enroll(target("delta"))
            assert [row["target_id"] for row in service.list_targets()] == ["beta", "delta", "gamma"]
            assert service.store.get_target("beta") == before_beta
            tomb = service.store.get_tombstone("alpha")
            assert tomb["connection_revision"] == 0 and tomb["audit_event_seq"] > 0
        finally:
            await service.shutdown()
    asyncio.run(run())


# 2 ---------------------------------------------------------------------
def test_history_and_artifacts_survive_removal_and_reopen(tmp_path):
    async def run():
        store_root = tmp_path / "store"
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Why?")
            service.store.update_investigation(row["investigation_id"], phase="needs_human",
                                               diagnosis={"summary": "old"})
            proposal = service.store.put_proposal({
                "target_id": "alpha", "investigation_id": row["investigation_id"],
                "status": "failed", "action": "restart_container"})
            artifact = service.store.save_artifact("proof.txt", b"proof-bytes", "text/plain")
            await service.disconnect("alpha")
            await service.remove_target("alpha", 0)
            assert service.store.get_investigation(row["investigation_id"])["diagnosis"] == {"summary": "old"}
            assert service.store.get_proposal(proposal["proposal_id"])["status"] == "failed"
            loaded = service.store.get_artifact(artifact["artifact_id"])
            assert Path(loaded["path"]).read_bytes() == b"proof-bytes"
            with pytest.raises(OpsError) as caught:
                await service.enroll(target())
            assert err_code(caught) == "target_id_retired"
        finally:
            await service.shutdown()
        reopened = OpsStore(store_root)
        try:
            assert reopened.get_investigation(row["investigation_id"]) is not None
            assert reopened.get_proposal(proposal["proposal_id"]) is not None
            assert reopened.is_retired("alpha")
            assert reopened.get_target("alpha") is None
            with pytest.raises(OpsError) as caught:
                reopened.enroll_target(target().model_dump(mode="json"))
            assert err_code(caught) == "target_id_retired"
        finally:
            reopened.close()
    asyncio.run(run())


# 3 ---------------------------------------------------------------------
def test_update_rejections_leave_record_unchanged(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            connected = service.store.get_target("alpha")
            with pytest.raises(OpsError) as caught:
                await service.update_target("alpha", 0, spec(name="New"))
            assert err_code(caught) == "target_must_be_disconnected"
            await service.disconnect("alpha")
            before = service.store.get_target("alpha")
            cases = [
                (spec(name="Other", target_id="beta"), "target_identity_immutable"),
                (spec(container_name="other-box"), "target_identity_immutable"),
                (spec(docker_context="other-ctx"), "target_identity_immutable"),
            ]
            for bad, code in cases:
                with pytest.raises(OpsError) as caught:
                    await service.update_target("alpha", 0, bad)
                assert err_code(caught) == code
            with pytest.raises(OpsError) as caught:
                await service.update_target("alpha", 9, spec(name="New"))
            assert err_code(caught) == "target_revision_changed"
            with pytest.raises(OpsError) as caught:
                await service.update_target("missing", 0, spec(name="missing"))
            assert err_code(caught) == "target_not_found"
            with pytest.raises(OpsError) as caught:
                await service.update_target("alpha", True, spec(name="New"))
            assert err_code(caught) == "target_revision_changed"
            with pytest.raises(OpsError):
                await service.update_target("alpha", 0, spec(monitoring=True, name="New"))
            with pytest.raises(OpsError) as caught:
                await service.update_target("alpha", 0, spec(policy=Policy(auto_investigate=True)))
            assert err_code(caught) == "enrollment_observe_only"
            with pytest.raises(OpsError) as caught:
                await service.update_target(
                    "alpha", 0, spec(policy=Policy(automatic_actions=["restart_container"])))
            assert err_code(caught) == "enrollment_observe_only"
            # Source validation failure: directory does not exist.
            with pytest.raises(OpsError):
                await service.update_target(
                    "alpha", 0, spec(source_dir=str(tmp_path / "nope"),
                                     source_revision="b" * 40))
            assert service.store.get_target("alpha") == before
            assert connected["target_id"] == "alpha"
        finally:
            await service.shutdown()
    asyncio.run(run())


# 4 ---------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["active", "pending", "dispatching", "unknown", "verifying_proposal", "rolling_back"])
def test_update_and_remove_blocked_by_durable_work(tmp_path, kind):
    async def run():
        service = make_service(tmp_path)
        try:
            await enroll_disconnected(service, target())
            # Fillers push the blocking record beyond any 50-row UI window.
            for index in range(55):
                filler = service.store.create_investigation("alpha", f"filler {index}")
                service.store.update_investigation(filler["investigation_id"], phase="needs_human")
            if kind == "active":
                row = service.store.create_investigation("alpha", "active")
                service.store.update_investigation(row["investigation_id"], phase="investigating")
                expected = "operation_busy"
            elif kind == "pending":
                row = service.store.create_investigation("alpha", "pending")
                service.store.update_investigation(row["investigation_id"], phase="awaiting_approval")
                service.store.put_proposal({"target_id": "alpha",
                    "investigation_id": row["investigation_id"], "status": "proposed",
                    "action": "restart_container"})
                expected = "operation_busy"
            elif kind in ("dispatching", "verifying_proposal", "rolling_back"):
                row = service.store.create_investigation("alpha", "work")
                service.store.update_investigation(row["investigation_id"], phase="needs_human")
                service.store.put_proposal({"target_id": "alpha",
                    "investigation_id": row["investigation_id"],
                    "status": {"dispatching": "dispatching", "verifying_proposal": "verifying",
                               "rolling_back": "rolling_back"}[kind],
                    "action": "restart_container"})
                expected = "operation_busy"
            else:
                row = service.store.create_investigation("alpha", "unknown work")
                service.store.update_investigation(row["investigation_id"], phase="needs_human")
                service.store.put_proposal({"target_id": "alpha",
                    "investigation_id": row["investigation_id"], "status": "unknown",
                    "action": "restart_container"})
                expected = "target_reconciliation_required"
            before = service.store.get_target("alpha")
            with pytest.raises(OpsError) as caught:
                await service.update_target("alpha", 0, spec(name="New"))
            assert err_code(caught) == expected
            with pytest.raises(OpsError) as caught:
                await service.remove_target("alpha", 0)
            assert err_code(caught) == expected
            assert service.store.get_target("alpha") == before
            # Unrelated idle target is not blocked.
            await enroll_disconnected(service, target("beta"))
            changed = await service.update_target("beta", 0, spec("beta", name="Beta2"))
            assert changed["connection_revision"] == 1
        finally:
            await service.shutdown()
    asyncio.run(run())


# 5 ---------------------------------------------------------------------
def test_valid_extension_increments_and_resets_current_state(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(spec(policy=Policy(restart_cooldown_seconds=3600)))
            row = service.store.create_investigation("alpha", "old diagnosis")
            service.store.update_investigation(row["investigation_id"], phase="needs_human",
                                               diagnosis={"summary": "kept"})
            service.store.put_setting("snapshot.alpha",
                                      {"snapshot_id": "s1", "compose": {"compose_file": "/x"}})
            current = service.store.get_target("alpha")
            service.store.put_target({**current, "verified_deployment": {"proposal_id": "p1"}})
            service.store.put_setting("lastrestart.alpha", 1234.5)
            service.observations["alpha"] = {"healthy": True}
            from collections import deque
            service.histories["alpha"] = deque([{"healthy": True}])
            service.bad_streak["alpha"] = 2
            await service.disconnect("alpha")
            root, revision, expected_hash = make_source(tmp_path)
            new = spec(name="Extended", source_dir=str(root), source_revision=revision,
                       patch_profile={"image": "sha256:" + "a" * 64,
                                      "test_paths": ["tests/test_app.py"]})
            view = await service.update_target("alpha", 0, new)
            assert view["connection_revision"] == 1
            assert view["name"] == "Extended"
            stored = service.store.get_target("alpha")
            assert stored["source_manifest_sha256"] == expected_hash
            assert stored["policy"] == {"automatic_actions": [], "auto_investigate": False,
                                        "restart_cooldown_seconds": 3600}
            assert stored["monitoring"] is False
            assert "verified_deployment" not in stored
            assert service.store.get_setting("snapshot.alpha") is None
            archive = service.store.get_setting("target_archive.alpha.0")
            assert archive["spec"]["target_id"] == "alpha"
            assert archive["snapshot"]["snapshot_id"] == "s1"
            assert service.store.get_setting("lastrestart.alpha") == 1234.5
            assert "alpha" not in service.observations
            assert "alpha" not in service.histories
            assert "alpha" not in service.bad_streak
            assert service.store.get_investigation(row["investigation_id"])["diagnosis"] == {"summary": "kept"}
            # True no-op keeps revision and baseline.
            service.store.put_setting("snapshot.alpha", {"snapshot_id": "s2", "compose": {}})
            current_spec = service.target("alpha")
            noop = await service.update_target("alpha", 1, current_spec)
            assert noop["connection_revision"] == 1
            assert service.store.get_setting("snapshot.alpha")["snapshot_id"] == "s2"
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_generation_zero_legacy_target_still_works(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            row = await service.start_investigation("alpha", "Check")
            assert row["connection_revision"] == 0
            await until(lambda: service.investigation(row["investigation_id"])["phase"] == "needs_human")
        finally:
            await service.shutdown()
    asyncio.run(run())


# 6 ---------------------------------------------------------------------
def test_old_generation_rejected_after_a_b_a_and_restart(tmp_path, monkeypatch):
    async def run():
        async def available():
            return {"available": True}
        monkeypatch.setattr(service_module, "runtime_info", available)
        docker = Docker()
        service = make_service(tmp_path, docker_factory=docker)
        try:
            original = spec()
            await service.enroll(original)
            old = service.store.create_investigation("alpha", "old")
            assert old["connection_revision"] == 0
            service.store.update_investigation(old["investigation_id"], phase="needs_human",
                diagnosis={"summary": "old", "action": "abstain"},
                patch={"patch_id": "p-old", "status": "tests_passed"},
                source_revision=None)
            # Executor proposal created in generation 0.
            runner_obs = observed(healthy=False)
            proposal = service.executor.create_proposal(
                service.target("alpha"), old["investigation_id"],
                {"action": "restart_container", "reason": "old failure"}, runner_obs)
            await service.disconnect("alpha")
            await service.update_target("alpha", 0, spec(name="Beta"))
            back = spec()  # identical profile to generation 0
            view = await service.update_target("alpha", 1, back)
            assert view["connection_revision"] == 2
            docker_reads = docker.reads
            calls_before = list(service.investigator.calls)
            with pytest.raises(OpsError) as caught:
                await service.prepare_patch(old["investigation_id"])
            assert err_code(caught) == "target_configuration_changed"
            with pytest.raises(OpsError) as caught:
                await service.prepare_deployment(old["investigation_id"])
            assert err_code(caught) == "target_configuration_changed"
            with pytest.raises(OpsError) as caught:
                service.executor.create_proposal(
                    service.target("alpha"), old["investigation_id"],
                    {"action": "restart_container", "reason": "old"}, runner_obs)
            assert err_code(caught) == "target_configuration_changed"
            claimed = service.store.transition_proposal(
                proposal["proposal_id"], "expired", "executing",
                authorization="user", approved_hash=proposal["proposal_hash"])
            # Orphan was expired by the update; force executing only to prove
            # the direct executor itself also refuses the old generation.
            if claimed is None:
                service.store.update_proposal(proposal["proposal_id"], status="executing",
                                               authorization="user",
                                               approved_hash=proposal["proposal_hash"])
                claimed = service.store.get_proposal(proposal["proposal_id"])
            with pytest.raises(OpsError) as caught:
                await service.executor.execute(claimed, service.target("alpha"), asyncio.Event())
            assert err_code(caught) == "target_configuration_changed"
            service.store.update_investigation(old["investigation_id"], phase="awaiting_approval")
            service.store.update_proposal(proposal["proposal_id"], status="proposed")
            with pytest.raises(OpsError) as caught:
                await service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
            assert err_code(caught) == "target_configuration_changed"
            service.store.update_investigation(old["investigation_id"], phase="needs_human")
            service.store.update_proposal(proposal["proposal_id"], status="executing",
                                           authorization="user",
                                           approved_hash=proposal["proposal_hash"])
            with pytest.raises(OpsError) as caught:
                await service.deployment_manager.build_proposal(
                    service.target("alpha"), old["investigation_id"], {}, runner_obs)
            assert err_code(caught) == "target_configuration_changed"
            with pytest.raises(OpsError) as caught:
                await service.promote_deployment(proposal["proposal_id"])
            assert err_code(caught) == "target_configuration_changed"
            assert docker.reads == docker_reads
            assert service.investigator.calls == calls_before
            await service.reconnect("alpha")
            new_row = await service.start_investigation("alpha", "fresh")
            assert new_row["connection_revision"] == 2
            await until(lambda: service.investigation(new_row["investigation_id"])["phase"] == "needs_human")
        finally:
            await service.shutdown()
        # Restart: the old generation is still rejected, unknowns preserved.
        service2 = make_service(tmp_path, docker_factory=Docker())
        try:
            assert service2.store.get_target("alpha")["connection_revision"] == 2
            with pytest.raises(OpsError) as caught:
                await service2.prepare_patch(old["investigation_id"])
            assert err_code(caught) == "target_configuration_changed"
        finally:
            await service2.shutdown()
    asyncio.run(run())


# 7 ---------------------------------------------------------------------
def test_duplicate_enroll_race_only_one_wins(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            results = await asyncio.gather(
                service.enroll(target()), service.enroll(target()),
                return_exceptions=True)
            ok = [item for item in results if not isinstance(item, Exception)]
            bad = [item for item in results if isinstance(item, OpsError)]
            assert len(ok) == 1 and len(bad) == 1
            assert bad[0].code == "target_already_enrolled"
            assert len(service.list_targets()) == 1
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_busy_appearing_before_commit_blocks_update(tmp_path, monkeypatch):
    import threading
    import aura_ops.source as source_module

    async def run():
        service = make_service(tmp_path)
        entered, release = threading.Event(), threading.Event()

        class BlockingIndex:
            def __init__(self, item):
                entered.set()
                release.wait(timeout=5)
                self.provenance_hash = "c" * 64

        monkeypatch.setattr(source_module, "SourceIndex", BlockingIndex)
        try:
            await enroll_disconnected(service, target())
            root, revision, _ = make_source(tmp_path)
            task = asyncio.create_task(service.update_target(
                "alpha", 0, spec(name="New", source_dir=str(root), source_revision=revision,
                                 patch_profile={"image": "sha256:" + "a" * 64,
                                                "test_paths": ["tests/test_app.py"]})))
            await asyncio.to_thread(entered.wait, 5)
            busy = service.store.create_investigation("alpha", "appeared")
            service.store.update_investigation(busy["investigation_id"], phase="investigating")
            release.set()
            with pytest.raises(OpsError) as caught:
                await task
            assert err_code(caught) == "operation_busy"
            assert service.store.get_target("alpha")["name"] == "Alpha"
            assert service.store.get_target("alpha")["connection_revision"] == 0
        finally:
            release.set()
            await service.shutdown()
    asyncio.run(run())


def test_policy_during_source_validation_prevents_stale_update(tmp_path, monkeypatch):
    import threading
    import aura_ops.source as source_module

    async def run():
        service = make_service(tmp_path)
        entered, release = threading.Event(), threading.Event()

        class BlockingIndex:
            def __init__(self, item):
                entered.set()
                release.wait(timeout=5)
                self.provenance_hash = "c" * 64

        monkeypatch.setattr(source_module, "SourceIndex", BlockingIndex)
        try:
            await enroll_disconnected(service, target())
            root, revision, _ = make_source(tmp_path)
            task = asyncio.create_task(service.update_target(
                "alpha", 0, spec(name="New", source_dir=str(root), source_revision=revision)))
            await asyncio.to_thread(entered.wait, 5)
            await service.set_policy("alpha", Policy(restart_cooldown_seconds=7200))
            release.set()
            with pytest.raises(OpsError) as caught:
                await task
            assert err_code(caught) == "target_revision_changed"
            stored = service.store.get_target("alpha")
            assert stored["name"] == "Alpha"
            assert stored["policy"]["restart_cooldown_seconds"] == 7200
            assert stored["connection_revision"] == 0
        finally:
            release.set()
            await service.shutdown()
    asyncio.run(run())


def test_stale_writes_after_removal_do_not_resurrect(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await enroll_disconnected(service, target())
            await service.remove_target("alpha", 0)
            with pytest.raises(OpsError) as caught:
                await service.set_policy("alpha", Policy(auto_investigate=True))
            assert err_code(caught) == "target_not_found"
            with pytest.raises(OpsError) as caught:
                await service.reconnect("alpha")
            assert err_code(caught) == "target_not_found"
            with pytest.raises(OpsError) as caught:
                await service.disconnect("alpha")
            assert err_code(caught) == "target_not_found"
            assert service.store.get_target("alpha") is None
            assert service.store.is_retired("alpha")
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_late_monitor_observation_is_discarded_after_removal(tmp_path, monkeypatch):
    async def run():
        async def available():
            return {"available": True}
        monkeypatch.setattr(service_module, "runtime_info", available)
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingDocker(Docker):
            async def collect(self):
                entered.set()
                await release.wait()
                return observed(healthy=False)

        service = make_service(tmp_path, docker_factory=BlockingDocker())
        try:
            await service.enroll(target())
            await service.enroll(target("beta"))
            service._monitor_task = asyncio.create_task(service._monitor())
            await entered.wait()
            await service.disconnect("alpha")
            await service.remove_target("alpha", 0)
            release.set()
            await asyncio.sleep(.05)
            assert service.store.get_target("alpha") is None
            assert service.store.is_retired("alpha")
            assert "alpha" not in service.observations
            assert service.bad_streak.get("alpha", 0) == 0
            assert not service._monitor_task.done()
        finally:
            release.set()
            await service.shutdown()
    asyncio.run(run())


# 8 ---------------------------------------------------------------------
def test_cooldown_blocks_removal_and_duplicate_pair_rejected(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(spec(policy=Policy(restart_cooldown_seconds=3600)))
            service.store.put_setting("lastrestart.alpha", time.time())
            stamp = service.store.get_setting("lastrestart.alpha")
            await service.disconnect("alpha")
            with pytest.raises(OpsError) as caught:
                await service.remove_target("alpha", 0)
            assert err_code(caught) == "restart_cooldown"
            changed = await service.update_target("alpha", 0, spec(name="Renamed"))
            assert changed["connection_revision"] == 1
            assert service.store.get_setting("lastrestart.alpha") == stamp
            assert service.target("alpha").policy.restart_cooldown_seconds == 3600
            with pytest.raises(OpsError) as caught:
                await service.remove_target("alpha", 1)
            assert err_code(caught) == "restart_cooldown"
            service.store.put_setting("lastrestart.alpha", time.time() - 7200)
            result = await service.remove_target("alpha", 1)
            assert result["removed"] is True
            # Duplicate live pair under a different id.
            await service.enroll(target("beta"))
            dup = spec("gamma", container_name="beta")
            with pytest.raises(OpsError) as caught:
                await service.enroll(dup)
            assert err_code(caught) == "target_already_enrolled"
        finally:
            await service.shutdown()
    asyncio.run(run())


# 9 / 10 ---------------------------------------------------------------
MINIMAL_CONFIGURATION = {
    "monitoring": {"configured": True, "missing": []},
    "restart": {"configured": True, "missing": []},
    "configuration_recovery": {"configured": False, "missing": ["compose_profile", "known_good_snapshot"]},
    "code_fix": {"configured": False, "missing": ["verified_source", "patch_profile"]},
    "deployment": {"configured": False, "missing": [
        "compose_profile", "known_good_snapshot", "verified_source", "source_image", "patch_profile",
        "deployment_root", "deployment_launch", "recovery_checks"]},
}


def test_configuration_summary_is_setup_only_and_consistent(tmp_path):
    async def run():
        docker = Docker()
        service = make_service(tmp_path, docker_factory=docker)
        try:
            await service.enroll(target())
            view = service.target_view("alpha")
            assert view["configuration"] == MINIMAL_CONFIGURATION
            assert view["connection_revision"] == 0
            assert service.list_targets()[0]["configuration"] == MINIMAL_CONFIGURATION
            assert service.snapshot()["targets"][0]["configuration"] == MINIMAL_CONFIGURATION
            reads = docker.reads
            service.list_targets()
            service.target_view("alpha")
            service.snapshot()
            assert docker.reads == reads
            # Full setup flips every section without granting execution.
            full = spec("beta", compose_file="/tmp/compose.json", compose_project="proj",
                        compose_service="svc", source_dir="/tmp/src",
                        source_revision="a" * 40, source_manifest_sha256="b" * 64,
                        source_image_id="sha256:" + "c" * 64,
                        patch_profile={"image": "sha256:" + "a" * 64, "test_paths": ["tests/t.py"]},
                        deployment_root="/app",
                        deployment_launch={"entrypoint": ["/usr/local/bin/server"], "command": []},
                        recovery_checks=[{"url": "http://127.0.0.1:9/dl",
                                          "expected_sha256": "c" * 64, "expected_bytes": 10}])
            service.store.put_target(full.model_dump(mode="json"))
            service.store.put_setting("snapshot.beta", {"compose": {"compose_file": "/tmp/compose.json"}})
            full_view = service.target_view("beta")
            assert all(section["configured"] for section in full_view["configuration"].values())
            assert all(section["missing"] == [] for section in full_view["configuration"].values())
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_setup_reports_missing_image_without_hiding_other_configured_tools():
    from aura_ops.target_management import build_configuration
    data = {"compose_file": "compose.yml", "compose_project": "proj", "compose_service": "svc",
            "source_dir": "/src", "source_revision": "a" * 40, "source_manifest_sha256": "b" * 64,
            "patch_profile": {}, "deployment_root": "/app", "deployment_launch": {}, "recovery_checks": [{}]}
    summary = build_configuration(data, {"compose": {"file": "compose.yml"}})
    assert summary["deployment"] == {"configured": False, "missing": ["source_image"]}
    assert summary["configuration_recovery"]["configured"]
    assert summary["code_fix"]["configured"]


@pytest.mark.parametrize("source_field", ["source_dir", "source_revision", "source_image_id"])
def test_source_bound_restore_setup_needs_reviewed_launch(source_field):
    from aura_ops.target_management import build_configuration
    data = {"compose_file": "compose.yml", "compose_project": "proj", "compose_service": "svc"}
    baseline = {"compose": {"file": "compose.yml"}}
    assert build_configuration(data, baseline)["configuration_recovery"]["configured"]
    data[source_field] = "present"
    assert build_configuration(data, baseline)["configuration_recovery"] == {
        "configured": False, "missing": ["deployment_launch"]}
    data["deployment_launch"] = {"entrypoint": ["server"]}
    assert build_configuration(data, baseline)["configuration_recovery"]["configured"]


def _http_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from aura_api.app import create_app

    async def unavailable():
        raise OpsError("docker_not_installed", "Docker is unavailable.", 503)
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    class OfflineInvestigator:
        ready = False
        async def close(self):
            pass

    class MissingDocker:
        def __init__(self, item, **kwargs):
            pass
        async def collect(self):
            raise OpsError("docker_not_installed", "Docker is unavailable.", 503)

    app = create_app(enable_ops=True, enable_demo=False, provider_configured=False,
                     ops_factory=lambda: OpsService(
                         tmp_path, docker_factory=MissingDocker,
                         investigator=OfflineInvestigator(), interval=.01))
    return TestClient, app


def _http_target(name="example"):
    return {"target_id": name, "name": name, "container_name": name,
            "probes": [{"url": "http://127.0.0.1:18090/"}]}


def test_http_update_delete_revision_configuration_and_origin(tmp_path, monkeypatch):
    TestClient, app = _http_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/api/v2/targets", json=_http_target()).status_code == 201
        listed = client.get("/api/v2/targets").json()
        assert listed[0]["connection_revision"] == 0
        assert listed[0]["configuration"] == MINIMAL_CONFIGURATION
        detail = client.get("/api/v2/targets/example").json()
        assert detail["configuration"] == listed[0]["configuration"]
        assert detail["connection_revision"] == 0
        status = client.get("/api/v2/status").json()
        assert status["targets"][0]["configuration"] == MINIMAL_CONFIGURATION
        with client.websocket_connect("/ws/ops") as ws:
            snapshot = ws.receive_json()
            assert snapshot["payload"]["targets"][0]["configuration"] == MINIMAL_CONFIGURATION
        # Connected update is rejected; cross-origin mutations write nothing.
        body = {"expected_revision": 0, "spec": {**_http_target(), "name": "New", "monitoring": False}}
        assert client.put("/api/v2/targets/example", json=body).status_code == 409
        client.post("/api/v2/targets/example/disconnect")
        evil = {"Origin": "https://untrusted.example"}
        assert client.put("/api/v2/targets/example", json=body, headers=evil).status_code == 403
        assert client.request("DELETE", "/api/v2/targets/example",
                              json={"expected_revision": 0}, headers=evil).status_code == 403
        assert client.get("/api/v2/targets/example").json()["name"] == "example"
        # Invalid bodies write nothing.
        assert client.put("/api/v2/targets/example", json={"expected_revision": True,
                          "spec": _http_target()}).status_code == 422
        assert client.put("/api/v2/targets/example", json={"spec": _http_target()}).status_code == 422
        assert client.put("/api/v2/targets/example", json={"expected_revision": 0,
                          "spec": _http_target(), "extra": 1}).status_code == 422
        updated = client.put("/api/v2/targets/example", json=body)
        assert updated.status_code == 200
        assert updated.json()["connection_revision"] == 1
        assert updated.json()["name"] == "New"
        assert updated.json()["configuration"] == MINIMAL_CONFIGURATION
        removed = client.request("DELETE", "/api/v2/targets/example", json={"expected_revision": 1})
        assert removed.status_code == 200
        assert removed.json() == {"target_id": "example", "removed": True,
                                  "history_retained": True, "container_stopped": False}
        assert client.get("/api/v2/targets/example").status_code == 404
        assert client.request("DELETE", "/api/v2/targets/example",
                              json={"expected_revision": 1}).status_code == 404
        assert client.post("/api/v2/targets", json=_http_target()).status_code == 409


def test_update_vs_remove_race_one_wins(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await enroll_disconnected(service, target())
            results = await asyncio.gather(
                service.update_target("alpha", 0, spec(name="Won")),
                service.remove_target("alpha", 0),
                return_exceptions=True)
            ok = [item for item in results if not isinstance(item, Exception)]
            bad = [item for item in results if isinstance(item, OpsError)]
            assert len(ok) == 1 and len(bad) == 1
            assert bad[0].code in ("target_revision_changed", "target_not_found")
            record = service.store.get_target("alpha")
            if record is not None:
                assert record["connection_revision"] == 1 and record["name"] == "Won"
                assert not service.store.is_retired("alpha")
            else:
                assert service.store.is_retired("alpha")
        finally:
            await service.shutdown()
    asyncio.run(run())
