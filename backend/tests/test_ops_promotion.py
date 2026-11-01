"""Promotion reuses real artifact validation; Docker boundaries are simulated."""
import asyncio
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_ops.actions import _hash
from aura_ops.models import OpsError, TargetSpec
from aura_ops.promotion import promote
from aura_ops.source import SourceIndex
from aura_ops.patches import _source_files
from test_ops_deployment import setup, proposal, approve, store, CANDIDATE_IMAGE


async def deployed(tmp_path, store):
    spec, runner, patch, manager = await setup(tmp_path, store)
    value = await proposal(spec, runner, patch, manager)
    await manager.execute(approve(store, value), spec, asyncio.Event())
    value = store.update_proposal(value["proposal_id"], status="verified", verification={
        "verified": True, "root_cause_fixed": True, "functional": {"verified": True}})
    row = {"investigation_id": "test-investigation", "target_id": spec.target_id,
           "created_at": "2026-10-28", "phase": "verified_fixed", "proposal_id": value["proposal_id"]}
    store._db.execute("INSERT INTO investigations VALUES (?,?,?,?)",
                      (row["investigation_id"], row["target_id"], row["created_at"], json.dumps(row)))
    class Docker:
        def __init__(self, *args, **kwargs):
            pass
        async def collect(self):
            return runner.observation()
        async def inspect_identity(self):
            return runner.observation()["identity"]
    manager.docker_factory = manager.actions.docker_factory = Docker
    original_runner = runner.__call__
    async def command(argv, **limits):
        if "compose" in argv and "config" in argv:
            path = Path(argv[argv.index("-f") + 1])
            document = json.loads(path.read_text())
            if "include" in document:
                from aura_ops.docker import CommandResult
                config = copy.deepcopy(runner.compose)
                overlay = json.loads(Path(document["include"][0]["path"][1]).read_text())
                config["services"]["app"].update(overlay["services"]["app"])
                runner.calls.append((argv, limits))
                return CommandResult(0, json.dumps(config))
        return await original_runner(argv, **limits)
    manager.runner = manager.actions.runner = command
    return spec, runner, patch, manager, value


def test_promotion_keeps_history_tests_and_identity_without_dispatch(tmp_path, store):
    async def run():
        spec, runner, patch, manager, value = await deployed(tmp_path, store)
        old_snapshot = store.get_setting("snapshot." + spec.target_id)
        source_bytes = Path(spec.source_dir, "app.py").read_bytes()
        result = await promote(manager, spec, value["proposal_id"])
        enrolled = TargetSpec.model_validate({k: v for k, v in store.get_target(spec.target_id).items()
                                              if k in TargetSpec.model_fields})
        assert enrolled.source_image_id == CANDIDATE_IMAGE
        assert enrolled.source_revision == spec.source_revision  # ancestry, not invented commit
        assert enrolled.source_dir != spec.source_dir
        assert Path(spec.source_dir, "app.py").read_bytes() == source_bytes
        assert "return 42" in SourceIndex(enrolled).read("app.py")["content"]
        assert SourceIndex(enrolled).inventory()["verified_overlay"]["patch_id"] == value["patch_id"]
        hashes, files = _source_files(enrolled, SourceIndex(enrolled))
        assert "aura_verified_tests/test_" + value["patch_id"] + ".py" in files
        assert "aura_verified_tests" in enrolled.patch_profile.test_paths
        assert result["previous_snapshot"] == old_snapshot
        assert result["snapshot_hash"] == _hash(store.get_setting("snapshot." + spec.target_id))
        assert len(runner.app_mutations) == 1  # original deployment only
        assert await promote(manager, enrolled, value["proposal_id"]) == result
        assert len(runner.app_mutations) == 1
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["unverified", "identity", "source", "policy", "unhealthy", "artifact"])
def test_promotion_refuses_unproven_or_changed_state_without_partial_commit(tmp_path, store, fault):
    async def run():
        spec, runner, patch, manager, value = await deployed(tmp_path, store)
        if fault == "unverified":
            store.update_proposal(value["proposal_id"], verification={"verified": True})
        elif fault == "identity":
            runner.app["Id"] = "9" * 64
        elif fault == "source":
            Path(patch["candidate_path"], "app.py").write_text("bad = 1")
        elif fault == "policy":
            spec.policy.auto_investigate = True
        elif fault == "unhealthy":
            original = runner.observation
            runner.observation = lambda: {**original(), "healthy": False}
        elif fault == "artifact":
            artifact = store.get_artifact(value["patch_metadata_id"])
            Path(artifact["path"]).write_text("{}")
        before = store.get_target(spec.target_id)
        snapshot = store.get_setting("snapshot." + spec.target_id)
        with pytest.raises(OpsError):
            await promote(manager, spec, value["proposal_id"])
        assert store.get_target(spec.target_id) == before
        assert store.get_setting("snapshot." + spec.target_id) == snapshot
        assert store.get_setting("promotion." + value["proposal_id"]) is None
        assert len(runner.app_mutations) == 1
    asyncio.run(run())


def test_promotion_cas_rejects_concurrent_policy_change(tmp_path, store):
    async def run():
        spec, runner, patch, manager, value = await deployed(tmp_path, store)
        old_snapshot = store.get_setting("snapshot." + spec.target_id)
        original = store.promote_baseline
        def race(*args):
            row = store.get_target(spec.target_id)
            row["monitoring"] = False
            store.put_target(row)
            return original(*args)
        store.promote_baseline = race
        with pytest.raises(OpsError, match="changed during promotion"):
            await promote(manager, spec, value["proposal_id"])
        assert store.get_target(spec.target_id)["monitoring"] is False
        assert store.get_target(spec.target_id)["source_image_id"] == spec.source_image_id
        assert store.get_setting("snapshot." + spec.target_id) == old_snapshot
    asyncio.run(run())
