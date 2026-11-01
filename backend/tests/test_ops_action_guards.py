"""OPS-ACTION-GUARD-001 regressions: hooks, shared cooldown, final binding."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aura_ops.actions import ActionExecutor
from aura_ops.docker import DockerTarget
from aura_ops.models import ActionKind, OpsError
from aura_ops.store import OpsStore
from test_ops_actions import FakeRunner, claim, create, expect_error, make_spec


@pytest.fixture
def store(tmp_path):
    instance = OpsStore(tmp_path / "ops")
    yield instance
    instance.close()


def _hooks_runner():
    return FakeRunner()


@pytest.mark.parametrize("action", [ActionKind.RESTORE, ActionKind.ROLLBACK])
@pytest.mark.parametrize("hook", ["post_start", "pre_stop"])
def test_recovery_hooks_rejected_at_capture_and_replay(store, tmp_path, action, hook):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        runner.config["services"]["bookmarks"][hook] = [{"command": "migrate"}]
        await expect_error("compose_lifecycle_unsupported",
                           executor.capture_snapshot(spec, runner.observation()))
        assert store.get_setting("snapshot.bookmarks") is None
        del runner.config["services"]["bookmarks"][hook]
        await executor.capture_snapshot(spec, runner.observation())
        if action == ActionKind.ROLLBACK:
            from test_ops_actions import OTHER_IMAGE
            runner.container["Image"] = OTHER_IMAGE
            runner.container["Config"]["Image"] = "linkding:new-build"
        runner.config["services"]["bookmarks"][hook] = [{"command": "migrate"}]
        proposal = claim(store, create(executor, spec, runner, action))
        await expect_error("compose_lifecycle_unsupported",
                           executor.execute(proposal, spec, asyncio.Event()))
        assert not runner.mutations
    asyncio.run(scenario())


def _dispatch_first(spec, runner, executor, store, action):
    async def go():
        proposal = claim(store, create(executor, spec, runner, action))
        return await executor.execute(proposal, spec, asyncio.Event())
    return asyncio.run(go())


def test_restart_blocks_restore_via_shared_cooldown(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True,
                                 automatic=[ActionKind.RESTART, ActionKind.RESTORE]), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        first = claim(store, create(executor, spec, runner, ActionKind.RESTART))
        await executor.execute(first, spec, asyncio.Event())
        assert len(runner.mutations) == 1
        assert store.get_setting("lastrestart.bookmarks") is not None
        second_proposal = create(executor, spec, runner, ActionKind.RESTORE)
        assert second_proposal["automation_eligible"] is False
        assert second_proposal["blocked_reason"] == "restart_cooldown"
        second = claim(store, second_proposal)
        await expect_error("restart_cooldown", executor.execute(second, spec, asyncio.Event()))
        assert len(runner.mutations) == 1
    asyncio.run(scenario())


def test_restore_blocks_rollback_via_shared_cooldown(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True,
                                 automatic=[ActionKind.RESTORE, ActionKind.ROLLBACK]), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        first = claim(store, create(executor, spec, runner, ActionKind.RESTORE), "policy")
        await executor.execute(first, spec, asyncio.Event())
        assert len(runner.mutations) == 1
        assert store.get_setting("lastrestart.bookmarks") is not None
        second_proposal = create(executor, spec, runner, ActionKind.ROLLBACK)
        assert second_proposal["automation_eligible"] is False
        assert second_proposal["blocked_reason"] == "restart_cooldown"
        second = claim(store, second_proposal)
        await expect_error("restart_cooldown", executor.execute(second, spec, asyncio.Event()))
        assert len(runner.mutations) == 1
    asyncio.run(scenario())


def test_rollback_blocks_restart_via_shared_cooldown(store, tmp_path):
    async def scenario():
        from test_ops_actions import OTHER_IMAGE
        spec, runner = make_spec(tmp_path, compose=True,
                                 automatic=[ActionKind.ROLLBACK, ActionKind.RESTART]), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        runner.container["Image"] = OTHER_IMAGE
        runner.container["Config"]["Image"] = "linkding:new-build"
        first = claim(store, create(executor, spec, runner, ActionKind.ROLLBACK))
        await executor.execute(first, spec, asyncio.Event())
        assert len(runner.mutations) == 1
        assert store.get_setting("lastrestart.bookmarks") is not None
        second_proposal = create(executor, spec, runner, ActionKind.RESTART)
        assert second_proposal["automation_eligible"] is False
        assert second_proposal["blocked_reason"] == "restart_cooldown"
        second = claim(store, second_proposal)
        await expect_error("restart_cooldown", executor.execute(second, spec, asyncio.Event()))
        assert len(runner.mutations) == 1
    asyncio.run(scenario())


def test_expired_cooldown_permits_valid_restore(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True,
                                 automatic=[ActionKind.RESTORE]), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        store.put_setting("lastrestart.bookmarks", time.time() - 700)
        proposal = create(executor, spec, runner, ActionKind.RESTORE)
        assert proposal["automation_eligible"] is True
        assert proposal["blocked_reason"] is None
        result = await executor.execute(claim(store, proposal, "policy"), spec, asyncio.Event())
        assert result["command_completed"] is True
        assert len(runner.mutations) == 1
    asyncio.run(scenario())


def test_proposal_creation_consumes_no_cooldown(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        assert store.get_setting("lastrestart.bookmarks") is None
        create(executor, spec, runner, ActionKind.RESTORE)
        assert store.get_setting("lastrestart.bookmarks") is None
        first = claim(store, create(executor, spec, runner, ActionKind.RESTART))
        await executor.execute(first, spec, asyncio.Event())
        stamped = store.get_setting("lastrestart.bookmarks")
        assert stamped is not None
        create(executor, spec, runner, ActionKind.RESTORE)
        assert store.get_setting("lastrestart.bookmarks") == stamped
    asyncio.run(scenario())


@pytest.mark.parametrize("action", [ActionKind.RESTORE, ActionKind.ROLLBACK])
def test_disruption_intent_persists_on_transport_error(store, tmp_path, action):
    async def scenario():
        from test_ops_actions import OTHER_IMAGE
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        if action == ActionKind.ROLLBACK:
            runner.container["Image"] = OTHER_IMAGE
            runner.container["Config"]["Image"] = "linkding:new-build"
        runner.fail_mutation = True
        proposal = claim(store, create(executor, spec, runner, action))
        await expect_error("action_outcome_unknown", executor.execute(proposal, spec, asyncio.Event()))
        assert len(runner.mutations) == 1
        assert store.get_setting("lastrestart.bookmarks") is not None
        assert store.get_proposal(proposal["proposal_id"])["status"] == "dispatching"
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["compose", "env", "snapshot"])
def test_config_change_during_final_identity_io_dispatches_zero(store, tmp_path, change):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        (tmp_path / ".env").write_text("PORT=9090", encoding="utf-8")
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        inspects = 0
        original = executor.runner

        async def tampering(argv, **limits):
            nonlocal inspects
            if argv[3:5] == ["container", "inspect"]:
                inspects += 1
                if inspects == 2:
                    if change == "compose":
                        Path(spec.compose_file).write_text("tampered", encoding="utf-8")
                    elif change == "env":
                        (tmp_path / ".env").write_text("PORT=9091", encoding="utf-8")
                    else:
                        # Change durable snapshot after proposal binding.
                        current = store.get_setting("snapshot.bookmarks")
                        tampered_snapshot = {**current, "snapshot_id": "0" * 32}
                        store.put_setting("snapshot.bookmarks", tampered_snapshot)
            return await original(argv, **limits)

        executor.runner = tampering
        with pytest.raises(OpsError) as caught:
            await executor.execute(proposal, spec, asyncio.Event())
        assert caught.value.code in ("config_changed", "snapshot_changed")
        assert not runner.mutations
        assert inspects >= 2
    asyncio.run(scenario())


def test_snapshot_change_during_automatic_observation_dispatches_zero(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True,
                                 automatic=[ActionKind.RESTORE]), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE), "policy")

        class TamperingCollect(DockerTarget):
            async def collect(self):
                observed = runner.observation(healthy=False)
                Path(spec.compose_file).write_text("tampered-during-collect", encoding="utf-8")
                return observed

        executor.docker_factory = TamperingCollect
        await expect_error("config_changed", executor.execute(proposal, spec, asyncio.Event()))
        assert not runner.mutations
    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["approval", "policy", "monitoring"])
def test_revocation_during_final_async_check_dispatches_zero(store, tmp_path, revocation):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        configs = 0
        original = executor.runner

        async def revoking(argv, **limits):
            nonlocal configs
            if argv[3] == "compose" and "config" in argv:
                configs += 1
                if configs == 2:
                    await asyncio.sleep(0)
                    if revocation == "approval":
                        store.update_proposal(proposal["proposal_id"], authorization="revoked")
                    elif revocation == "policy":
                        changed = spec.model_copy(deep=True)
                        changed.policy.auto_investigate = True
                        store.put_target(changed.model_dump(mode="json"))
                    else:
                        store.put_target({**spec.model_dump(mode="json"), "monitoring": False})
            return await original(argv, **limits)

        executor.runner = revoking
        with pytest.raises(OpsError) as caught:
            await executor.execute(proposal, spec, asyncio.Event())
        assert caught.value.code in ("action_not_authorized", "policy_changed",
                                     "target_disconnected", "proposal_changed",
                                     "action_already_claimed")
        assert not runner.mutations
        assert configs == 2
    asyncio.run(scenario())


def test_cancellation_during_final_async_check_dispatches_zero(store, tmp_path):
    async def scenario():
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        proposal = claim(store, create(executor, spec, runner, ActionKind.RESTORE))
        event = asyncio.Event()
        configs = 0
        original = executor.runner

        async def cancelling(argv, **limits):
            nonlocal configs
            if argv[3] == "compose" and "config" in argv:
                configs += 1
                if configs == 2:
                    await asyncio.sleep(0)
                    event.set()
            return await original(argv, **limits)

        executor.runner = cancelling
        with pytest.raises(asyncio.CancelledError):
            await executor.execute(proposal, spec, event)
        assert not runner.mutations
        assert store.get_setting("lastrestart.bookmarks") is None
        assert configs == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("action", [ActionKind.RESTORE, ActionKind.ROLLBACK])
def test_unchanged_legitimate_config_dispatches_exactly_once(store, tmp_path, action):
    async def scenario():
        from test_ops_actions import OTHER_IMAGE
        spec, runner = make_spec(tmp_path, compose=True), FakeRunner()
        executor = ActionExecutor(store, runner=runner)
        await executor.capture_snapshot(spec, runner.observation())
        if action == ActionKind.ROLLBACK:
            runner.container["Image"] = OTHER_IMAGE
            runner.container["Config"]["Image"] = "linkding:new-build"
        proposal = claim(store, create(executor, spec, runner, action))
        result = await executor.execute(proposal, spec, asyncio.Event())
        assert result["command_completed"] is True
        assert len(runner.mutations) == 1
    asyncio.run(scenario())
