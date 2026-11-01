"""Acceptance runner must prove the current early-block contract without bypasses."""
import asyncio
import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[1])]
from tools.ops.acceptance import CaseFail, RunnerError, check_cooldown_block, run_matrix, validate_cases


@pytest.mark.parametrize("cases", [[], [4, 5], [1, 1, 4], [1, 5, 4], [1, 7], [True, 4]])
def test_selection_requires_unique_ordered_cases_with_enrollment(cases):
    with pytest.raises(RunnerError):
        validate_cases(cases)


def test_current_cooldown_contract_and_negative_controls():
    row = {"phase": "needs_human", "error": {"code": "restart_cooldown"}}
    proposal = {"action": "restart_container", "status": "blocked", "blocked_reason": "restart_cooldown"}
    container = {"Id": "owned", "Image": "pinned", "State": {"StartedAt": "first-restart"}}
    check_cooldown_block(row, proposal, container, container, "recorded-time", "recorded-time")
    mutations = [
        ("row", "phase", "awaiting_approval"), ("row", "error", {"code": "other"}),
        ("proposal", "status", "failed"), ("proposal", "blocked_reason", None),
        ("proposal", "action", "restore_config_snapshot"),
        ("proposal", "dispatch_attempted_at", "attempted"), ("proposal", "authorization", "user"),
        ("after", "Id", "replacement"), ("after", "Image", "other"),
        ("after", "State", {"StartedAt": "second-restart"}),
    ]
    for obj, key, value in mutations:
        changed = {"row": copy.deepcopy(row), "proposal": copy.deepcopy(proposal), "after": copy.deepcopy(container)}
        changed[obj][key] = value
        with pytest.raises(CaseFail):
            check_cooldown_block(changed["row"], changed["proposal"], container, changed["after"], "time", "time")
    with pytest.raises(CaseFail):
        check_cooldown_block(row, proposal, container, container, "time", "changed")
    with pytest.raises(CaseFail):
        check_cooldown_block(row, proposal, container, container, None, None)


def test_selected_matrix_does_not_run_port_fault_or_modify_cooldown():
    async def exercise():
        calls = []
        async def baseline():
            return {}
        async def stop():
            calls.append("shutdown")
        def case(number):
            async def execute():
                calls.append(number)
                return {"measured": True}
            return execute
        acc = SimpleNamespace(cases=[], write_result=lambda result: None)
        matrix = SimpleNamespace(baseline=baseline, service=SimpleNamespace(shutdown=stop),
                                 store=SimpleNamespace(list_proposals=lambda: []))
        for number, name in enumerate(("enroll_snapshot", "healthy_control", "wrong_port",
                                       "reduced_memory", "restart_cooldown", "reopen"), 1):
            setattr(matrix, "case_" + name, case(number))
        result = {"provisioning": {}, "mutation_trace": {}}
        await run_matrix(matrix, acc, result, [1, 4, 5, 6])
        assert calls == [1, 4, 5, 6, "shutdown"]
        assert [entry["case"] for entry in result["cases"]] == [1, 4, 5, 6]
        assert all(entry["passed"] for entry in result["cases"])
    asyncio.run(exercise())
