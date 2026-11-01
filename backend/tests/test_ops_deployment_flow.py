"""Controller gates around independently tested patch/build executors."""
import asyncio

import pytest

from test_ops_service import Docker, make_service, observed, target, until


class Deployment:
    def __init__(self, docker):
        self.docker = docker
        self.deploys = 0
        self.rollbacks = 0

    async def execute(self, proposal, spec, cancel):
        self.deploys += 1
        self.docker.state = observed(image="c")
        return {"identity": self.docker.state["identity"], "command_completed": True, "verified": False}

    async def rollback(self, proposal, spec, cancel):
        self.rollbacks += 1
        self.docker.state = observed(image="b")
        return {"identity": self.docker.state["identity"], "command_completed": True, "verified": False}


@pytest.mark.parametrize("outcome", ["passed", "failed", "changed", "cancelled"])
def test_only_independent_success_certifies_fix_and_failure_rolls_back(tmp_path, outcome):
    async def run():
        docker = Docker()
        deployment = Deployment(docker)
        checked = []
        async def functional(spec, cancel, **kwargs):
            checked.append(spec.target_id)
            if outcome == "cancelled":
                cancel.set()
                raise asyncio.CancelledError()
            return {"verified": outcome in ("passed", "changed"), "reason": outcome,
                    "identity": {key: value for key, value in observed(image="d" if outcome == "changed" else "c")["identity"].items() if key != "image_ref"}}
        service = make_service(tmp_path, docker_factory=docker, deployment_manager=deployment,
                               functional_verifier=functional)
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Fix the measured defect")
            service.store.update_investigation(row["investigation_id"], phase="awaiting_approval")
            proposal = service.store.put_proposal({"target_id": "alpha", "investigation_id": row["investigation_id"],
                "action": "deploy_verified_patch", "proposal_hash": "a" * 64, "rollback_on_failure": True})
            assert deployment.deploys == 0
            await service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
            await until(lambda: service._task and service._task.done())
            final = service.investigation(row["investigation_id"])
            assert deployment.deploys == 1
            assert checked == ["alpha"]
            if outcome == "passed":
                assert final["phase"] == "verified_fixed"
                assert final["verification"]["root_cause_fixed"] is True
                assert deployment.rollbacks == 0
            else:
                assert final["phase"] == "needs_human"
                assert not (final.get("verification") or {}).get("root_cause_fixed")
                assert deployment.rollbacks == (0 if outcome == "cancelled" else 1)
        finally:
            await service.shutdown()
    asyncio.run(run())


def test_failed_availability_never_runs_expensive_functional_workload(tmp_path):
    async def run():
        docker = Docker()
        deployment = Deployment(docker)
        async def execute(proposal, spec, cancel):
            docker.state = observed(healthy=False, image="c")
            return {"identity": docker.state["identity"]}
        deployment.execute = execute
        async def functional(*args, **kwargs):
            pytest.fail("No load should be sent to an unavailable deployment")
        service = make_service(tmp_path, docker_factory=docker, deployment_manager=deployment,
                               functional_verifier=functional)
        try:
            await service.enroll(target())
            row = service.store.create_investigation("alpha", "Fix")
            service.store.update_investigation(row["investigation_id"], phase="awaiting_approval")
            proposal = service.store.put_proposal({"target_id": "alpha", "investigation_id": row["investigation_id"],
                "action": "deploy_verified_patch", "proposal_hash": "a" * 64, "rollback_on_failure": True})
            await service.decide(proposal["proposal_id"], proposal["proposal_hash"], True)
            await until(lambda: service._task and service._task.done())
            assert service.investigation(row["investigation_id"])["phase"] == "needs_human"
            assert deployment.rollbacks == 1
        finally:
            await service.shutdown()
    asyncio.run(run())
