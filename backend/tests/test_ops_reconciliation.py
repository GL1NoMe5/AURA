"""OPS-RECONCILE-001: evidence-bound operator review of unknown outcomes (offline)."""
from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aura_ops.models import OpsError
from aura_ops.reconciliation import ATTESTATION_TEXT
from aura_ops.store import OpsStore, proposal_content_hash
from test_ops_service import Docker, Investigator, make_service as _make_service, observed as _observed, target, until


def observed(**kwargs):
    value = _observed(**kwargs)
    value["config"] = {"memory_limit_bytes": 100_000_000, "published_ports": {}, "server_port": None}
    return value


def make_service(path, **kwargs):
    # Use the complete config emitted by the real Docker collector.
    kwargs.setdefault("docker_factory", Docker(observed()))
    return _make_service(path, **kwargs)


def put_unknown(store, target_id="alpha", proposal_id="unknown-1", image="b", **extra):
    record = {"proposal_id": proposal_id, "target_id": target_id, "status": "unknown",
              "action": "restart_container", "expected_identity": observed(image=image)["identity"],
              "error": "action_outcome_unknown", **extra}
    return store.put_proposal(record)


def confirm_kwargs(review, note="Inspected the rack display; old command ended."):
    return {"review_hash": review["review_hash"], "attestation": ATTESTATION_TEXT,
            "operator_note": note}


def test_status_reads_reflect_gate_outside_50_row_history(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            put_unknown(service.store, proposal_id="very-old-unknown")
            for index in range(60):
                service.store.create_investigation("alpha", f"Recent {index}")
            assert len(service.store.list_investigations("alpha", limit=50)) == 50
            [entry] = service.list_targets()
            assert entry["reconciliation_required"] is True
            assert entry["unresolved_unknown_ids"] == ["very-old-unknown"]
            [snap_entry] = service.snapshot()["targets"]
            assert snap_entry["reconciliation_required"] is True
            assert snap_entry["unresolved_unknown_ids"] == ["very-old-unknown"]
            from aura_ops.actions import _check_reconciliation
            with pytest.raises(OpsError) as blocked:
                _check_reconciliation(service.store, "alpha")
            assert blocked.value.code == "target_reconciliation_required"
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_report_alone_healthy_waiting_and_missing_attestation_never_unlock(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            put_unknown(service.store)
            review = await service.collect_reconciliation("alpha")
            assert review["healthy"] is True
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
            from aura_ops.actions import _check_reconciliation
            with pytest.raises(OpsError):
                _check_reconciliation(service.store, "alpha")
            with pytest.raises(OpsError) as missing:
                await service.confirm_reconciliation(
                    "alpha", review["review_id"], review_hash=review["review_hash"],
                    attestation="I looked at it.", operator_note="note")
            assert missing.value.code == "attestation_invalid"
            with pytest.raises(OpsError) as empty_note:
                await service.confirm_reconciliation(
                    "alpha", review["review_id"], **{**confirm_kwargs(review), "operator_note": "  "})
            assert empty_note.value.code == "invalid_operator_note"
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
            assert service.store.get_review(review["review_id"])["status"] == "pending"
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_valid_exact_review_confirms_once_and_resets_scope(tmp_path):
    async def run():
        from aura_ops.models import ActionKind, Policy
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            await service.enroll(target("beta"))
            await service.set_policy("alpha", Policy(auto_investigate=True,
                                                     automatic_actions=[ActionKind.RESTART]))
            beta_policy = service.store.get_target("beta")["policy"]
            unknown = put_unknown(service.store, dispatch_attempted_at="2026-10-29T00:00:00+00:00")
            service.store.put_proposal({"proposal_id": "stale-pending", "target_id": "alpha",
                                        "status": "proposed", "action": "restart_container"})
            service.store.put_proposal({"proposal_id": "beta-pending", "target_id": "beta",
                                        "status": "proposed", "action": "restart_container"})
            service.store.put_setting("lastrestart.alpha", 1234.5)
            service.store.put_setting("snapshot.alpha", {"marker": "baseline"})
            row = service.store.create_investigation("alpha", "Earlier work")
            service.store.update_investigation(row["investigation_id"], phase="needs_human")
            review = await service.collect_reconciliation("alpha")
            assert review["unknowns"] == [{"proposal_id": unknown["proposal_id"],
                                           "action": "restart_container",
                                           "content_hash": proposal_content_hash(unknown)}]
            result = await service.confirm_reconciliation("alpha", review["review_id"],
                                                          **confirm_kwargs(review))
            assert result["result"] == "outcome_reconciled"
            assert result["idempotent_replay"] is False
            assert result["reconciled_ids"] == [unknown["proposal_id"]]
            assert result["expired_proposals"] == ["stale-pending"]
            assert result["remaining_gate"] == {"reconciliation_required": False,
                                                "unresolved_unknown_ids": []}
            assert "verified" not in json.dumps(result) and "mitigated" not in json.dumps(result)
            kept = service.store.get_proposal(unknown["proposal_id"])
            assert kept["status"] == "unknown"
            assert kept["error"] == "action_outcome_unknown"
            assert kept["dispatch_attempted_at"] == "2026-10-29T00:00:00+00:00"
            assert service.store.get_proposal("stale-pending")["status"] == "expired"
            assert service.store.get_proposal("beta-pending")["status"] == "proposed"
            alpha_policy = service.store.get_target("alpha")["policy"]
            assert alpha_policy["auto_investigate"] is False
            assert alpha_policy["automatic_actions"] == []
            assert alpha_policy["restart_cooldown_seconds"] == 600
            assert service.store.get_target("beta")["policy"] == beta_policy
            assert service.store.get_setting("lastrestart.alpha") == 1234.5
            assert service.store.get_setting("snapshot.alpha") == {"marker": "baseline"}
            assert service.investigation(row["investigation_id"])["phase"] == "needs_human"
            events = [event for event in service.store.events_since(0)
                      if event["type"] == "reconciliation_confirmed"]
            assert len(events) == 1 and events[0]["seq"] == result["event_seq"]
            assert events[0]["payload"]["result"] == "outcome_reconciled"
            assert service.store.get_review(review["review_id"])["status"] == "consumed"
            assert service.investigator.calls == []
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_new_or_changed_unknown_reblocks_and_replay_cannot_dismiss(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            first = put_unknown(service.store, proposal_id="unknown-a")
            review = await service.collect_reconciliation("alpha")
            await service.confirm_reconciliation("alpha", review["review_id"],
                                                 **confirm_kwargs(review))
            assert service.store.list_unresolved_unknowns("alpha") == []
            put_unknown(service.store, proposal_id="unknown-b")
            replay = await service.confirm_reconciliation("alpha", review["review_id"],
                                                          **confirm_kwargs(review))
            assert replay["idempotent_replay"] is True
            assert replay["reconciled_ids"] == [first["proposal_id"]]
            assert replay["remaining_gate"] == {"reconciliation_required": True,
                                                "unresolved_unknown_ids": ["unknown-b"]}
            from aura_ops.actions import _check_reconciliation
            with pytest.raises(OpsError) as blocked:
                _check_reconciliation(service.store, "alpha")
            assert blocked.value.code == "target_reconciliation_required"
        finally:
            await service.shutdown()
        reopened = OpsStore(tmp_path / "store")
        try:
            assert reopened.get_review(review["review_id"])["status"] == "consumed"
            assert [item["proposal_id"] for item in reopened.list_unresolved_unknowns("alpha")] == ["unknown-b"]
        finally:
            reopened.close()
    asyncio.run(run())


def test_wrong_hash_target_and_request_faults_reject(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            await service.enroll(target("beta"))
            put_unknown(service.store)
            review = await service.collect_reconciliation("alpha")
            with pytest.raises(OpsError) as wrong_hash:
                await service.confirm_reconciliation(
                    "alpha", review["review_id"],
                    **{**confirm_kwargs(review), "review_hash": "0" * 64})
            assert wrong_hash.value.code == "review_hash_mismatch"
            with pytest.raises(OpsError) as wrong_target:
                await service.confirm_reconciliation("beta", review["review_id"],
                                                     **confirm_kwargs(review))
            assert wrong_target.value.code == "review_not_found"
            with pytest.raises(OpsError) as missing:
                await service.confirm_reconciliation("alpha", "no-such-review",
                                                     **confirm_kwargs(review))
            assert missing.value.code == "review_not_found"
            with pytest.raises(OpsError) as missing_get:
                service.get_reconciliation("beta", review["review_id"])
            assert missing_get.value.code == "review_not_found"
            assert service.get_reconciliation("alpha", review["review_id"])["review_id"] == review["review_id"]
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
        finally:
            await service.shutdown()
    asyncio.run(run())


class ScriptedDocker:
    """Fake read-only boundary returning a scripted observation sequence."""

    def __init__(self, states, error=None, hook=None):
        self.states = [deepcopy(state) for state in states]
        self.error = error
        self.hook = hook
        self.reads = 0

    def __call__(self, spec, **kwargs):
        return self

    async def collect(self):
        self.reads += 1
        if self.hook:
            self.hook(self.reads)
        if self.error:
            raise self.error
        state = deepcopy(self.states[min(self.reads - 1, len(self.states) - 1)])
        from aura_ops.models import utc_now as _now
        state["timestamp"] = _now()
        return state

    async def inspect_identity(self):
        return deepcopy(self.states[0]["identity"])


def test_timeout_cancel_stale_and_restarted_report_reject(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            put_unknown(service.store)
            review = await service.collect_reconciliation("alpha")
            cancelled = asyncio.Event()
            cancelled.set()
            with pytest.raises(asyncio.CancelledError):
                await service.confirm_reconciliation("alpha", review["review_id"],
                                                     **confirm_kwargs(review), cancel=cancelled)
            with pytest.raises(asyncio.CancelledError):
                await service.collect_reconciliation("alpha", cancel=cancelled)
            stale = dict(review, expires_at="2000-01-01T00:00:00+00:00")
            service.store.save_review(stale)
            with pytest.raises(OpsError) as expired:
                await service.confirm_reconciliation("alpha", review["review_id"],
                                                     **confirm_kwargs(review))
            assert expired.value.code == "review_expired"
            assert service.store.get_review(review["review_id"])["status"] == "expired"
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
        finally:
            await service.shutdown()
    asyncio.run(run())

    async def hanging():
        class HangingDocker:
            def __call__(self, spec, **kwargs):
                return self

            async def collect(self):
                await asyncio.sleep(30)

        service = make_service(tmp_path / "hang", docker_factory=HangingDocker(),
                               reconciliation_timeout=0.05)
        try:
            await service.enroll(target())
            put_unknown(service.store)
            with pytest.raises(OpsError) as timed_out:
                await service.collect_reconciliation("alpha")
            assert timed_out.value.code == "reconciliation_collection_timeout"
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
        finally:
            await service.shutdown()
    asyncio.run(hanging())

    async def restart():
        first = make_service(tmp_path / "restart")
        try:
            await first.enroll(target())
            put_unknown(first.store)
            review = await first.collect_reconciliation("alpha")
        finally:
            await first.shutdown()
        second = make_service(tmp_path / "restart")
        try:
            await second.startup()
            assert second.store.get_review(review["review_id"])["status"] == "expired"
            with pytest.raises(OpsError) as stale_report:
                await second.confirm_reconciliation("alpha", review["review_id"],
                                                    **confirm_kwargs(review))
            assert stale_report.value.code == "review_expired"
            fresh = await second.collect_reconciliation("alpha")
            result = await second.confirm_reconciliation("alpha", fresh["review_id"],
                                                         **confirm_kwargs(fresh))
            assert result["result"] == "outcome_reconciled"
            assert result["remaining_gate"]["reconciliation_required"] is False
        finally:
            await second.shutdown()
    asyncio.run(restart())


def test_incomplete_unstable_unsupported_and_spec_changes_reject(tmp_path):
    async def scenario(name, states, code, *, error=None, mutate=None):
        service = make_service(tmp_path / name, docker_factory=ScriptedDocker(states, error=error))
        try:
            await service.enroll(target())
            put_unknown(service.store)
            if mutate == "collect":
                with pytest.raises(OpsError) as rejected:
                    await service.collect_reconciliation("alpha")
                assert rejected.value.code == code
            else:
                review = await service.collect_reconciliation("alpha")
                if mutate == "policy":
                    from aura_ops.models import Policy
                    await service.set_policy("alpha", Policy(auto_investigate=True))
                elif mutate == "proposal":
                    service.store.update_proposal("unknown-1", verification={"attempt": 1})
                elif mutate == "active":
                    service.store.create_investigation("alpha", "Queued work")
                with pytest.raises(OpsError) as rejected:
                    await service.confirm_reconciliation("alpha", review["review_id"],
                                                         **confirm_kwargs(review))
                assert rejected.value.code == code
            assert len(service.store.list_unresolved_unknowns("alpha")) == 1
        finally:
            await service.shutdown()

    async def run():
        base = observed()
        no_identity = {key: value for key, value in base.items() if key != "identity"}
        partial = dict(deepcopy(base), collection_errors={"logs": "docker_timeout"})
        moved = deepcopy(base)
        moved["identity"] = dict(base["identity"], started_at="2026-10-29T00:00:00Z")
        reconfig = deepcopy(base)
        reconfig["config"]["memory_limit_bytes"] = 1
        foreign = observed(image="c")
        await scenario("missing-identity", [no_identity, no_identity], "identity_missing",
                       mutate="collect")
        await scenario("partial-collection", [partial, partial], "observation_incomplete",
                       mutate="collect")
        await scenario("docker-down", [base, base], "docker_unavailable",
                       error=OpsError("docker_unavailable", "Engine gone.", 503), mutate="collect")
        await scenario("moving-target", [base, moved], "target_changed", mutate="collect")
        await scenario("moving-config", [base, reconfig], "config_changed", mutate="collect")
        await scenario("foreign-image", [foreign, foreign], "reconciliation_image_unrecognized",
                       mutate="collect")
        await scenario("policy-moved", [base, base], "reconciliation_state_changed",
                       mutate="policy")
        await scenario("proposal-moved", [base, base], "reconciliation_state_changed",
                       mutate="proposal")
        await scenario("active-work", [base, base], "reconciliation_active_operation",
                       mutate="active")
        queued = make_service(tmp_path / "queued-collect")
        try:
            await queued.enroll(target())
            put_unknown(queued.store)
            queued.store.create_investigation("alpha", "Queued work")
            with pytest.raises(OpsError) as busy:
                await queued.collect_reconciliation("alpha")
            assert busy.value.code == "reconciliation_active_operation"
        finally:
            await queued.shutdown()
    asyncio.run(run())


def test_unknown_and_target_race_during_confirm_collection_rejects_atomically(tmp_path):
    async def scenario(name, racer, code):
        service = make_service(tmp_path / name)
        docker = ScriptedDocker([observed(), observed()], hook=racer(service))
        service.docker_factory = docker
        service.executor.docker_factory = docker
        try:
            await service.enroll(target())
            put_unknown(service.store)
            service.store.put_proposal({"proposal_id": "stale-pending", "target_id": "alpha",
                                        "status": "proposed", "action": "restart_container"})
            before_target = deepcopy(service.store.get_target("alpha"))
            review = await service.collect_reconciliation("alpha")
            # Arm the race only for the confirmation's own awaited collection.
            docker.reads = 0
            docker.hook = racer(service, armed=True)
            with pytest.raises(OpsError) as rejected:
                await service.confirm_reconciliation("alpha", review["review_id"],
                                                     **confirm_kwargs(review))
            assert rejected.value.code == code
            assert service.store.get_target("alpha") == before_target or name == "target-race"
            if name == "target-race":
                assert service.store.get_target("alpha")["policy"]["auto_investigate"] is True
            assert service.store.get_proposal("stale-pending")["status"] == "proposed"
            assert service.store.get_review(review["review_id"])["status"] == "pending"
            assert not [event for event in service.store.events_since(0)
                        if event["type"] == "reconciliation_confirmed"]
            assert [item["proposal_id"] for item in service.store.list_unresolved_unknowns("alpha")]
        finally:
            await service.shutdown()

    def unknown_racer(service, armed=False):
        def hook(reads):
            if armed and reads == 1:
                put_unknown(service.store, proposal_id="racing-unknown")
        return hook

    def target_racer(service, armed=False):
        def hook(reads):
            if armed and reads == 1:
                record = service.store.get_target("alpha")
                record["policy"]["auto_investigate"] = True
                service.store.put_target(record)
        return hook

    asyncio.run(scenario("unknown-race", unknown_racer, "reconciliation_state_changed"))
    asyncio.run(scenario("target-race", target_racer, "reconciliation_state_changed"))


def test_second_store_instance_race_rejects_with_no_partial_writes(tmp_path):
    async def run():
        service = make_service(tmp_path)
        second = OpsStore(tmp_path / "store")
        try:
            await service.enroll(target())
            put_unknown(service.store)
            service.store.put_proposal({"proposal_id": "stale-pending", "target_id": "alpha",
                                        "status": "proposed", "action": "restart_container"})
            before_target = deepcopy(service.store.get_target("alpha"))
            review = await service.collect_reconciliation("alpha")
            second.put_proposal({"proposal_id": "other-instance-unknown", "target_id": "alpha",
                                 "status": "unknown", "action": "restart_container",
                                 "expected_identity": observed()["identity"]})
            with pytest.raises(OpsError) as rejected:
                await service.confirm_reconciliation("alpha", review["review_id"],
                                                     **confirm_kwargs(review))
            assert rejected.value.code == "reconciliation_state_changed"
            assert service.store.get_target("alpha") == before_target
            assert service.store.get_proposal("stale-pending")["status"] == "proposed"
            assert service.store.get_review(review["review_id"])["status"] == "pending"
            assert not [event for event in service.store.events_since(0)
                        if event["type"] == "reconciliation_confirmed"]
            assert sorted(item["proposal_id"]
                          for item in service.store.list_unresolved_unknowns("alpha")) == [
                              "other-instance-unknown", "unknown-1"]
        finally:
            second.close()
            await service.shutdown()
    asyncio.run(run())


def test_unhealthy_never_labelled_recovered_and_unrelated_target_works(tmp_path):
    async def run():
        sick = observed(healthy=False)
        service = make_service(tmp_path, docker_factory=ScriptedDocker([sick, sick]))
        try:
            await service.enroll(target())
            await service.enroll(target("beta"))
            put_unknown(service.store)
            review = await service.collect_reconciliation("alpha")
            assert review["healthy"] is False
            result = await service.confirm_reconciliation("alpha", review["review_id"],
                                                          **confirm_kwargs(review))
            assert result["result"] == "outcome_reconciled"
            blob = json.dumps(result)
            assert "recovered" not in blob and "verified" not in blob
            assert "mitigated" not in blob and "fixed" not in blob
            beta = [entry for entry in service.list_targets() if entry["target_id"] == "beta"][0]
            assert beta["reconciliation_required"] is False
            assert beta["unresolved_unknown_ids"] == []
            with pytest.raises(OpsError) as nothing:
                await service.collect_reconciliation("beta")
            assert nothing.value.code == "reconciliation_not_required"
            from aura_ops.actions import _check_reconciliation
            _check_reconciliation(service.store, "beta")
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_redacted_report_binds_full_unknown_set_without_secrets(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            first = put_unknown(service.store, proposal_id="unknown-a")
            second = put_unknown(service.store, proposal_id="unknown-b", action="rollback_image")
            review = await service.collect_reconciliation("alpha")
            assert review["target_created_at"] == service.store.get_target("alpha")["created_at"]
            assert review["spec"]["target_id"] == "alpha"
            assert review["docker_context"] == "default"
            assert [entry["proposal_id"] for entry in review["unknowns"]] == ["unknown-a", "unknown-b"]
            assert {entry["proposal_id"]: entry["content_hash"] for entry in review["unknowns"]} == {
                "unknown-a": proposal_content_hash(first), "unknown-b": proposal_content_hash(second)}
            blob = json.dumps(review)
            assert "environment" not in blob and "Env" not in blob
            assert review["notices"]["no_proof"].startswith("Stable snapshots DO NOT prove")
            assert "observe-only" in review["notices"]["observe_only"]
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_http_contract_with_fake_boundaries_and_provider_disabled(tmp_path, monkeypatch):
    import aura_ops.service as service_module
    from aura_api.app import create_app
    from fastapi.testclient import TestClient

    async def unavailable():
        raise OpsError("docker_not_installed", "Docker is unavailable.", 503)
    monkeypatch.setattr(service_module, "runtime_info", unavailable)

    class OfflineInvestigator:
        ready = False

        async def close(self):
            pass

    state = observed()
    factory = ScriptedDocker([state, state])

    def ops_factory():
        from aura_ops.service import OpsService
        return OpsService(tmp_path, docker_factory=factory, investigator=OfflineInvestigator(),
                          interval=10.0)

    app = create_app(enable_ops=True, enable_demo=False, provider_configured=False,
                     ops_factory=ops_factory)
    with TestClient(app) as client:
        assert client.post("/api/v2/targets", json={
            "target_id": "alpha", "name": "Alpha", "container_name": "alpha",
            "probes": [{"url": "http://127.0.0.1:18090/health"}]}).status_code == 201
        app.state.ops.store.put_proposal(
            {"proposal_id": "unknown-1", "target_id": "alpha", "status": "unknown",
             "action": "restart_container", "expected_identity": observed()["identity"]})
        assert client.get("/api/v2/targets/alpha").json()["reconciliation_required"] is True
        assert client.get("/api/v2/status").json()["targets"][0]["unresolved_unknown_ids"] == ["unknown-1"]
        collected = client.post("/api/v2/targets/alpha/reconciliations")
        assert collected.status_code == 201
        review = collected.json()
        assert client.get(f"/api/v2/targets/alpha/reconciliations/{review['review_id']}").json() == review
        assert client.get("/api/v2/targets/alpha/reconciliations/nope").status_code == 404
        good = {"review_hash": review["review_hash"], "attestation": ATTESTATION_TEXT,
                "operator_note": "Checked the live container; old command ended."}
        assert client.post(f"/api/v2/targets/alpha/reconciliations/{review['review_id']}/confirm",
                           json={**good, "review_hash": "0" * 64}).status_code == 409
        assert client.post(f"/api/v2/targets/alpha/reconciliations/{review['review_id']}/confirm",
                           json={**good, "attestation": "yes"}).status_code == 400
        assert client.post(f"/api/v2/targets/alpha/reconciliations/{review['review_id']}/confirm",
                           json={**good, "evidence": {"forged": True}}).status_code == 422
        confirmed = client.post(f"/api/v2/targets/alpha/reconciliations/{review['review_id']}/confirm",
                                json=good)
        assert confirmed.status_code == 200
        assert confirmed.json()["result"] == "outcome_reconciled"
        assert client.get("/api/v2/targets/alpha").json()["reconciliation_required"] is False
        events = client.get("/api/v2/events", params={"after": 0}).json()
        assert any(event["type"] == "reconciliation_confirmed" for event in events)


def test_new_action_possible_after_reconciliation_without_reusing_old(tmp_path):
    async def run():
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_ops_actions import FakeRunner, claim, create, make_spec
        from aura_ops.actions import ActionExecutor

        runner = FakeRunner()
        base = runner.observation()
        docker_state = {"timestamp": base["timestamp"], "healthy": True,
                        "identity": dict(base["identity"]),
                        "runtime": {"running": True, "status": "running"},
                        "resources": {"memory_bytes": 1, "memory_limit_bytes": 8},
                        "probes": [{"url": "http://127.0.0.1:9090/", "ok": True,
                                    "status_code": 200, "duration_ms": 1}],
                        "logs": "", "config": {"server_port": None, **base["config"]}}
        service = make_service(tmp_path, docker_factory=ScriptedDocker([docker_state, docker_state]))
        try:
            spec = make_spec(tmp_path)
            service.store.put_target(spec.model_dump(mode="json"))
            executor = ActionExecutor(service.store, runner=runner)
            old = executor.create_proposal(spec, "inv-old", {
                "action": "restart_container", "reason": "Measured outage."},
                runner.observation(healthy=False))
            service.store.update_proposal(old["proposal_id"], status="unknown",
                                          error="action_outcome_unknown")
            review = await service.collect_reconciliation("bookmarks")
            result = await service.confirm_reconciliation("bookmarks", review["review_id"],
                                                          **confirm_kwargs(review))
            assert result["remaining_gate"]["reconciliation_required"] is False
            fresh = executor.create_proposal(spec, "inv-new", {
                "action": "restart_container", "reason": "Fresh measured outage."},
                runner.observation(healthy=False))
            assert fresh.get("blocked_reason") is None
            assert fresh["proposal_id"] != old["proposal_id"]
            outcome = await executor.execute(claim(service.store, fresh), spec, asyncio.Event())
            assert outcome["command_completed"] is True
            assert len(runner.mutations) == 1
            assert service.store.get_proposal(old["proposal_id"])["status"] == "unknown"
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("missing", ["config", "memory", "health_type"])
def test_incomplete_values_cannot_support_confirmation(tmp_path, missing):
    async def run():
        state = observed()
        if missing == "config":
            state["config"] = {}
        elif missing == "memory":
            state["resources"]["memory_bytes"] = None
        else:
            state["healthy"] = 1
        service = make_service(tmp_path, docker_factory=ScriptedDocker([state]))
        try:
            await service.enroll(target())
            put_unknown(service.store)
            with pytest.raises(OpsError, match="required|incomplete"):
                await service.collect_reconciliation("alpha")
            assert service.store.list_unresolved_unknowns("alpha")
        finally:
            await service.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("at_commit", [False, True])
def test_orphan_inflight_proposal_blocks_review_and_atomic_commit(tmp_path, at_commit):
    async def run():
        service = make_service(tmp_path)
        other = OpsStore(tmp_path / "store")
        try:
            await service.enroll(target())
            put_unknown(service.store)
            def add_inflight():
                other.put_proposal({"proposal_id": "inflight-without-investigation", "target_id": "alpha",
                                    "status": "dispatching", "action": "restart_container"})
            if at_commit:
                review = await service.collect_reconciliation("alpha")
                original = service.store.confirm_reconciliation_review
                def race(**kwargs):
                    add_inflight()
                    return original(**kwargs)
                service.store.confirm_reconciliation_review = race
                with pytest.raises(OpsError) as rejected:
                    await service.confirm_reconciliation("alpha", review["review_id"], **confirm_kwargs(review))
                assert service.store.get_review(review["review_id"])["status"] == "pending"
            else:
                add_inflight()
                with pytest.raises(OpsError) as rejected:
                    await service.collect_reconciliation("alpha")
            assert rejected.value.code == "reconciliation_active_operation"
            assert service.store.list_unresolved_unknowns("alpha")
            assert not any(e["type"] == "reconciliation_confirmed" for e in service.store.events_since(0))
        finally:
            other.close()
            await service.shutdown()
    asyncio.run(run())


def test_operator_note_is_redacted_before_durable_audit_and_publish(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            put_unknown(service.store)
            review = await service.collect_reconciliation("alpha")
            queue = service.subscribe()
            await service.confirm_reconciliation("alpha", review["review_id"],
                **confirm_kwargs(review, note="Checked. password=dummy-sensitive-fixture"))
            for record in [service.store.get_review(review["review_id"]), service.store.events_since(0), queue.get_nowait()]:
                assert "dummy-sensitive-fixture" not in json.dumps(record)
                assert "[REDACTED]" in json.dumps(record)
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_target_changes_while_collecting_report_reject_old_evidence(tmp_path):
    async def run():
        service = make_service(tmp_path)
        try:
            await service.enroll(target())
            put_unknown(service.store)
            def change(reads):
                if reads == 1:
                    record = service.store.get_target("alpha")
                    record["docker_context"] = "other-engine"
                    service.store.put_target(record)
            service.docker_factory = ScriptedDocker([observed()], hook=change)
            with pytest.raises(OpsError) as rejected:
                await service.collect_reconciliation("alpha")
            assert rejected.value.code == "reconciliation_state_changed"
            assert service.store.list_reviews("alpha") == []
        finally:
            await service.shutdown()
    asyncio.run(run())
