"""A blocked recovery explains the waiting period without reviving old consent."""
import asyncio
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_ops.models import OpsError, Policy
from test_ops_service import Investigator, make_service, observed, target, until


@pytest.mark.parametrize("action", ["restart_container", "restore_config_snapshot", "rollback_image"])
def test_cooldown_guidance_and_new_check_after_wait(tmp_path, monkeypatch, action):
    class RecommendsRecovery(Investigator):
        async def diagnose(self, spec, evidence, cancel, emit, refresh=None):
            return {"diagnosis": {"summary": "A permitted recovery is suggested.",
                                  "action": action, "reason": "Observed app failure.",
                                  "findings": [], "needs_source_analysis": False},
                    "evidence": evidence, "calls": []}

    async def run():
        service = make_service(tmp_path, investigator=RecommendsRecovery())
        try:
            await service.enroll(target())
            await service.set_policy("alpha", Policy(restart_cooldown_seconds=900))
            service.store.put_setting("snapshot.alpha", {
                "compose": {"file": "fixture.json"}, "identity": observed()["identity"],
                "provenance": {"verified": True}})
            now = time.time()
            service.store.put_setting("lastrestart.alpha", now)
            monkeypatch.setattr("aura_ops.actions.time.time", lambda: now + 30)
            first = await service.start_investigation("alpha", "Check recovery")
            await until(lambda: service.investigation(first["investigation_id"])["phase"] == "needs_human")
            row = service.investigation(first["investigation_id"])
            assert row["error"]["code"] == "restart_cooldown"
            assert "15 minutes" in row["error"]["message"]
            assert "new check" in row["error"]["message"]
            assert "baseline" not in row["error"]["message"]
            old = service.store.get_proposal(row["proposal_id"])
            assert old["status"] == "blocked"
            marker = service.store.get_setting("lastrestart.alpha")

            monkeypatch.setattr("aura_ops.actions.time.time", lambda: now + 901)
            with pytest.raises(OpsError) as caught:
                await service.decide(old["proposal_id"], old["proposal_hash"], True)
            assert caught.value.code == "proposal_not_pending"
            second = await service.start_investigation("alpha", "Check the current state again")
            await until(lambda: service.investigation(second["investigation_id"])["phase"] == "awaiting_approval")
            fresh = service.store.get_proposal(service.investigation(second["investigation_id"])["proposal_id"])
            assert fresh["proposal_id"] != old["proposal_id"]
            assert fresh["blocked_reason"] is None and fresh["status"] == "proposed"
            assert service.store.get_setting("lastrestart.alpha") == marker
        finally:
            await service.shutdown()
    asyncio.run(run())
