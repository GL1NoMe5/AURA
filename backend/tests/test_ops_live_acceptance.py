"""Offline tests for the real-Docker acceptance runner.

No Docker, no network, no provider. Docker boundaries are faked; production
validation models (RoleOutput/Diagnosis/TargetSpec) are real.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_ops.investigator import RoleOutput
from aura_ops.models import Diagnosis, OpsError
import tools.ops.acceptance as acceptance
from tools.ops.acceptance import (
    Acceptance, CaseFail, DeterministicAdapter, RunnerError, dispatch_records,
    fault_mem_override, fault_port_override, healthchecks_compose,
    linkding_compose, owner_ok, pick_ports, run_case, wait_phase,
)


RUN = "acc-test0123456"


@pytest.mark.parametrize('wal_state', ['empty', 'committed'])
def test_real_sqlite_copy_preserves_wal_transactions(tmp_path, wal_state):
    import sqlite3
    from tools.ops.check_candidate import COPY
    source, dest = tmp_path / 'source', tmp_path / 'dest'
    source.mkdir(); dest.mkdir()
    db = sqlite3.connect(source / 'db.sqlite3')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE evidence (value TEXT)')
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if wal_state == 'committed':
            db.execute("INSERT INTO evidence VALUES ('only-in-wal')")
            db.commit()
            assert (source / 'db.sqlite3-wal').stat().st_size > 0
        else:
            assert (source / 'db.sqlite3-wal').stat().st_size == 0
        before = {p.name: p.read_bytes() for p in source.iterdir() if not p.name.endswith('-shm')}
        exec(COPY, {'SOURCE': str(source), 'DEST': str(dest)})
        assert before == {p.name: p.read_bytes() for p in source.iterdir() if not p.name.endswith('-shm')}
        clone = sqlite3.connect(dest / 'db.sqlite3')
        try:
            assert clone.execute('SELECT value FROM evidence').fetchall() == (
                [('only-in-wal',)] if wal_state == 'committed' else [])
            assert clone.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        finally:
            clone.close()
    finally:
        db.close()


def test_sqlite_copy_rejects_lost_nonempty_wal(tmp_path, monkeypatch):
    import shutil
    import sqlite3
    from tools.ops.check_candidate import COPY
    source, dest = tmp_path / 'source', tmp_path / 'dest'
    source.mkdir(); dest.mkdir()
    db = sqlite3.connect(source / 'db.sqlite3')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE evidence (value TEXT)')
        db.commit()
        original = shutil.copy2
        monkeypatch.setattr(shutil, 'copy2', lambda src, dst: None if str(src).endswith('-wal') else original(src, dst))
        with pytest.raises(RuntimeError, match='differs before SQLite validation'):
            exec(COPY, {'SOURCE': str(source), 'DEST': str(dest)})
    finally:
        db.close()


def container_row(labels):
    return {"Id": "a" * 64, "Config": {"Labels": labels}}


def test_owner_labels_exact_match_only():
    assert owner_ok("container", container_row({"org.aura.accept": RUN}), RUN)
    assert owner_ok("network", {"Labels": {"org.aura.accept": RUN}}, RUN)
    assert owner_ok("volume", {"Labels": {"org.aura.accept": RUN}}, RUN)
    assert not owner_ok("container", container_row({"org.aura.accept": "other"}), RUN)
    assert not owner_ok("container", container_row({}), RUN)
    assert not owner_ok("network", {}, RUN)
    with pytest.raises(RunnerError):
        owner_ok("image", {}, RUN)


def test_pick_ports_skips_occupied_and_refuses_shortage():
    assert pick_ports(set()) == [18093, 18094]
    assert pick_ports({18093, 18095}) == [18094, 18096]
    with pytest.raises(RunnerError):
        pick_ports({18093, 18094, 18095})


def test_compose_keeps_apps_internal_and_gateways_bound():
    names = {"run": RUN, "app": "app", "gateway": "gw", "volume": "vol",
             "internal": "int", "ingress": "ing", "project": "proj"}
    linkding = linkding_compose(Path("/tmp"), names, 18093)
    app, gateway = linkding["services"]["linkding"], linkding["services"]["gateway"]
    assert "ports" not in app
    assert app["networks"] == ["internal"]
    assert app["labels"] == {"org.aura.accept": RUN}
    assert gateway["ports"] == ["127.0.0.1:18093:8080"]
    assert gateway["read_only"] is True and gateway["cap_drop"] == ["ALL"]
    assert gateway["volumes"] == [str(acceptance.ROOT / "tools/ops/lab_gateway.py") + ":/gateway.py:ro"]
    hc = healthchecks_compose(names, 18094, "hc-image", "/run/hc/.env")
    hc_app = hc["services"]["healthchecks"]
    assert "ports" not in hc_app and hc_app["networks"] == ["internal"]
    assert hc_app["env_file"] == ["/run/hc/.env"]
    assert "SECRET_KEY=" not in json.dumps(hc)
    assert linkding["volumes"]["data"] == {"external": True, "name": "vol"}


def test_fault_overrides_touch_only_targeted_config():
    port = fault_port_override()["services"]["linkding"]
    assert set(port) == {"command", "environment"}
    assert "9091" in " ".join(port["command"]) and port["environment"] == {"LD_SERVER_PORT": "9091"}
    mem = fault_mem_override()["services"]["linkding"]
    assert mem == {"mem_limit": "96m", "memswap_limit": "96m"}


def evidence_packet():
    return {"observation.current": {"identity": {"container_id": "a" * 64, "started_at": "t"},
                                    "probes": [{"url": "http://127.0.0.1:18093/health",
                                                "status_code": 200, "ok": True}]},
            "target.baseline": {"snapshot_id": "s"}}


def messages(system, packet):
    return [{"role": "system", "content": system},
            {"role": "user", "content": "EVIDENCE DATA:\n" + json.dumps(packet)}]


@pytest.mark.parametrize("mode,action", [("abstain", "abstain"),
                                         ("restore", "restore_config_snapshot"),
                                         ("restart", "restart_container")])
def test_deterministic_adapter_validates_through_production_models(mode, action):
    async def check():
        adapter = DeterministicAdapter(mode)
        assert adapter.ready is True
        role = await adapter.complete(messages("Assess resources.", evidence_packet()))
        RoleOutput.model_validate_json(role.content, strict=True)
        assert role.requested_model == role.response_model == "deterministic_mock"
        synthesis = await adapter.complete(messages("Synthesize the evidence.", evidence_packet()))
        diagnosis = Diagnosis.model_validate_json(synthesis.content, strict=True)
        assert diagnosis.action.value == action
        if action != "abstain":
            assert any(f.confidence == "supported" for f in diagnosis.findings)
            refs = {ref for f in diagnosis.findings for ref in f.evidence_refs}
            assert refs <= set(evidence_packet())
        await adapter.aclose()
        assert adapter.closed is True
    asyncio.run(check())


def test_deterministic_adapter_rejects_unknown_mode():
    with pytest.raises(RunnerError):
        DeterministicAdapter("deploy")


def test_default_offline_runs_no_mutations(tmp_path, monkeypatch, capsys):
    # All preflight files belong to this test, not the developer's private lab.
    import tools.ops.acceptance as preflight_module
    fixture_files = {
        "docker-placeholder": "", 
        "resources/ops/healthchecks.json": json.dumps({"image": "sha256:" + "b" * 64}),
        ".local/ops-lab/source-manifest.json": json.dumps({"revision": preflight_module.LINKDING_REVISION, "files": {}}),
        ".local/ops-lab/credentials.env": "",
        ".local/ops-healthchecks/credentials.env": "",
        ".local/ops-healthchecks/.env": "",
    }
    for name, content in fixture_files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(preflight_module, "ROOT", tmp_path)
    monkeypatch.setattr(preflight_module, "DOCKER", str(tmp_path / "docker-placeholder"))
    calls = []
    labels = {"org.aura.accept": "other-run"}

    def fake(argv, timeout=30, input_text=None):
        calls.append(argv)
        verb = argv[3:5]
        assert verb[0] not in ("create", "up", "exec", "rm", "start", "stop", "restart", "run", "build")
        if argv[3] == "version":
            return 0, '{"Version":"29.0","Os":"linux"}', ""
        if verb == ["image", "inspect"]:
            return 0, json.dumps([{"Id": argv[-1]}]), ""
        if argv[3] == "ps":
            return 0, "", ""
        if verb == ["container", "inspect"]:
            return 0, json.dumps([{"Id": "a" * 64, "Image": "sha256:" + "b" * 64,
                                   "State": {"StartedAt": "t"}, "Config": {"Labels": labels}}]), ""
        raise AssertionError(argv)

    monkeypatch.setattr(acceptance, "execute", fake)
    monkeypatch.setattr(acceptance, "port_occupied", lambda port: False)
    monkeypatch.setattr(Acceptance, "backend_health", lambda self: 200)
    assert acceptance.main([]) == 0
    assert calls, "preflight must probe docker read-only"
    assert all(argv[1:3] == ["--context", "desktop-linux"] for argv in calls)


def test_cleanup_order_containers_networks_volumes_and_leak_free(tmp_path):
    removed = []

    def fake(argv, timeout=30, input_text=None):
        if argv[4] == "inspect":
            labels = {"Labels": {"org.aura.accept": RUN}}
            if argv[3] == "container":
                return 0, json.dumps([{"Config": labels}]), ""
            return 0, json.dumps([labels]), ""
        if argv[4] == "rm":
            removed.append((argv[3], argv[-1]))
            return 0, "", ""
        if argv[4] in ("ps", "ls"):
            assert argv[3] in ("container", "network", "volume")
            return 0, "", ""
        raise AssertionError(argv)

    acc = Acceptance(RUN, cmd=fake)
    acc.created = [("network", "n1"), ("volume", "v1"), ("container", "c1")]
    assert acc.cleanup() == []
    assert removed == [("container", "c1"), ("network", "n1"), ("volume", "v1")]
    assert all(item["removed"] and item["label_verified"] for item in acc.cleanup_records)


def test_cleanup_refuses_foreign_resource():
    def fake(argv, timeout=30, input_text=None):
        if argv[4] == "inspect":
            return 0, json.dumps([{"Config": {"Labels": {}}}]), ""
        if argv[4] in ("ps", "ls"):
            assert argv[3] in ("container", "network", "volume")
            return 0, "leftover\n", ""
        raise AssertionError(argv)

    acc = Acceptance(RUN, cmd=fake)
    acc.created = [("container", "c1")]
    leaks = acc.cleanup()
    assert acc.cleanup_records == [{"kind": "container", "id": "c1", "removed": False,
                                    "error": "RunnerError: Cleanup ownership mismatch for container c1"}]
    assert leaks, "leftover labelled resources must be reported"


def test_copy_mounts_source_readonly_and_compares_fingerprints():
    def fake(argv, timeout=30, input_text=None):
        if argv[3:5] == ["container", "create"]:
            return 0, "d" * 64 + "\n", ""
        if argv[3:6] == ["container", "start", "--attach"]:
            return 0, 'ACCEPT_BEFORE={"db.sqlite3": "abc"}\nACCEPT_AFTER={"source": {"db.sqlite3": "abc"}, "dest": {"db.sqlite3": "abc"}}', ""
        if argv[3:5] == ["container", "inspect"]:
            return 0, json.dumps([{"Config": {"Labels": {"org.aura.accept": RUN}},
                                   "State": {"ExitCode": 0}}]), ""
        if argv[3:5] == ["container", "rm"]:
            return 0, "", ""
        raise AssertionError(argv)

    acc = Acceptance(RUN, cmd=fake)
    result = acc.copy_volume("src-vol", "dst-vol", "img")
    assert result == {"fingerprints": {"db.sqlite3": "abc"}, "files": 1, "attempts": 1}
    creates = [argv for argv in acc.fixture_commands if argv[3:5] == ["container", "create"]]
    assert len(creates) == 1
    assert "type=volume,source=src-vol,target=/source,readonly" in creates[0]
    assert any("target=/dest" in part and "readonly" not in part for part in creates[0])
    assert "integrity_check" in acceptance.COPY
    assert acceptance.COPY in acceptance.FUSED
    assert "ACCEPT_BEFORE=" in creates[0][-1] and "ACCEPT_AFTER=" in creates[0][-1]
    assert acc.created == []


def test_copy_rejects_fingerprint_mismatch_after_bounded_attempts():
    starts = []

    def fake(argv, timeout=30, input_text=None):
        if argv[3:5] == ["container", "create"]:
            return 0, "e" * 64 + "\n", ""
        if argv[3:6] == ["container", "start", "--attach"]:
            starts.append(argv)
            return 0, 'ACCEPT_BEFORE={"a": "1"}\nACCEPT_AFTER={"source": {"a": "1"}, "dest": {"a": "2"}}', ""
        if argv[3:5] == ["container", "inspect"]:
            return 0, json.dumps([{"Config": {"Labels": {"org.aura.accept": RUN}},
                                   "State": {"ExitCode": 0}}]), ""
        if argv[3:5] == ["container", "rm"]:
            return 0, "", ""
        raise AssertionError(argv)

    with pytest.raises(RunnerError, match="Stable copy failed after 4 attempts"):
        Acceptance(RUN, cmd=fake).copy_volume("src-vol", "dst-vol", "img")
    assert len(starts) == 4


def test_start_failure_carries_container_output_tail():
    def fake(argv, timeout=30, input_text=None):
        if argv[3:6] == ["container", "start", "--attach"]:
            return 1, "partial stdout\n", "Traceback (most recent call last):\nRuntimeError: boom\n"
        raise AssertionError(argv)

    acc = Acceptance(RUN, cmd=fake)
    with pytest.raises(RunnerError, match="boom"):
        acc.start_attached("f" * 64, 5)
    try:
        acc.start_attached("f" * 64, 5)
    except RunnerError as exc:
        assert "partial stdout" in str(exc) and "boom" in str(exc)
    assert acc.fixture_commands[0][3:6] == ["container", "start", "--attach"]


def test_run_case_records_expected_actual_and_outcomes(tmp_path):
    async def check():
        from aura_ops.models import OpsError as RealOpsError
        acc = Acceptance(RUN)
        acc.dir = tmp_path
        result = {"cases": []}

        async def ok():
            return {"value": 1}

        async def fail():
            raise CaseFail({"value": 2}, "mismatch")

        async def gate():
            raise RealOpsError("restart_cooldown", "cooling down", 409)

        await run_case(acc, result, 1, "ok-case", "expect one", ok)
        await run_case(acc, result, 2, "fail-case", "expect one", fail)
        await run_case(acc, result, 3, "gate-case", "expect pass", gate)
        assert [case["passed"] for case in result["cases"]] == [True, False, False]
        assert result["cases"][1]["error"] == "mismatch"
        assert result["cases"][2]["blocked"] is True
        assert result["cases"][2]["actual"] == {"product_gate": "restart_cooldown"}
        saved = json.loads((tmp_path / "result.json").read_text())
        assert len(saved["cases"]) == 3
    asyncio.run(check())


def test_wait_phase_settles_or_times_out(tmp_path):
    async def check():
        from aura_ops.store import OpsStore
        store = OpsStore(tmp_path / "store")
        try:
            store.put_target({"target_id": "t"})
            row = store.create_investigation("t", "question")
            service = SimpleNamespace(_task=None)
            settled = await wait_phase(store, service, row["investigation_id"], {"queued"}, 1)
            assert settled["phase"] == "queued"
            with pytest.raises(CaseFail):
                await wait_phase(store, service, row["investigation_id"], {"mitigated"}, 0.1)
        finally:
            store.close()
    asyncio.run(check())


def test_dispatch_records_exclude_reasons_and_secrets():
    store = SimpleNamespace(list_proposals=lambda: [{"proposal_id": "p", "action": "restart_container",
        "status": "executed", "target_id": "t", "investigation_id": "i",
        "dispatch_attempted_at": "ts", "authorization": "user",
        "reason": "secret-should-never-appear"}])
    records = dispatch_records(store)
    assert records == [{"proposal_id": "p", "action": "restart_container", "status": "executed",
                        "target_id": "t", "investigation_id": "i", "dispatch_attempted_at": "ts",
                        "authorization": "user"}]
    assert "secret-should-never-appear" not in json.dumps(records)


def test_matrix_budget_enforced(tmp_path):
    from tools.ops.acceptance import Matrix
    acc = Acceptance(RUN)
    acc.dir = tmp_path
    matrix = Matrix(acc, {"cases": []}, {"ports": [18093, 18094]}, "hc-image")
    try:
        matrix.deadline = 0.0
        with pytest.raises(CaseFail, match="deadline"):
            matrix.ensure_budget()
    finally:
        matrix.store.close()

@pytest.mark.parametrize('foreign', [False, True])
def test_cleanup_tracks_recreated_exact_name_and_rejects_foreign(foreign):
    removed = []
    current = {'ID': 'b' * 64, 'Names': 'exact-owned-app'}
    def fake(argv, timeout=30, input_text=None):
        if argv[3:5] == ['container', 'ls'] and '--filter' not in argv:
            return 0, json.dumps(current), ''
        if argv[3:5] == ['container', 'inspect']:
            return 0, json.dumps([container_row({'org.aura.accept': 'other' if foreign else RUN})]), ''
        if argv[4] == 'rm':
            removed.append(argv[-1])
            return 0, '', ''
        if '--filter' in argv:
            return 0, '', ''
        raise AssertionError(argv)
    acc = Acceptance(RUN, cmd=fake)
    acc.created = [('container', 'a' * 64)]
    acc.compose_containers = {'exact-owned-app'}
    assert acc.cleanup() == []
    assert removed == ([] if foreign else ['b' * 64])
    assert any(item.get('already_absent') for item in acc.cleanup_records)
    assert all(item['removed'] for item in acc.cleanup_records) is not foreign


def test_baseline_builds_real_target_contracts_without_docker(tmp_path, monkeypatch):
    async def check():
        from tools.ops.acceptance import Matrix
        config = tmp_path / '.local/ops-healthchecks/target.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'probes': [{'url': 'http://127.0.0.1:18091/checks/', 'credential_env': 'AURA_TARGET_HEALTHCHECKS_COOKIE'}]}))
        monkeypatch.setattr(acceptance, 'ROOT', tmp_path)
        acc = Acceptance(RUN); acc.dir = tmp_path / 'run'
        prov = {'ports': [18093,18094], 'linkding': {'app': 'sandbox-ld', 'project': 'sandbox-ld'},
                'hc': {'app': 'sandbox-hc'}, 'linkding_file': str(tmp_path / 'compose.json')}
        matrix = Matrix(acc, {}, prov, 'hc-image')
        async def healthy(spec):
            assert spec.container_name in ('sandbox-ld','sandbox-hc')
            return {'healthy': True}
        async def noop(*args): pass
        monkeypatch.setattr(matrix, 'collect', healthy)
        monkeypatch.setattr(matrix.service, 'startup', noop)
        monkeypatch.setattr(acceptance.asyncio, 'sleep', noop)
        try:
            assert await matrix.baseline() == {'cookie_refreshed': False}
            assert matrix.linkding_spec.source_revision == acceptance.LINKDING_REVISION
            assert matrix.hc_spec.probes[1].url == 'http://127.0.0.1:18094/checks/'
        finally:
            await matrix.service.shutdown()
    asyncio.run(check())


def test_reopen_accepts_disconnect_that_explicitly_does_not_stop_apps(tmp_path, monkeypatch):
    async def check():
        from aura_ops.models import TargetSpec
        from tools.ops.acceptance import Matrix
        monkeypatch.setattr(acceptance, 'ROOT', tmp_path)
        acc = Acceptance(RUN); acc.dir = tmp_path / 'run'
        prov = {'ports': [18093,18094], 'linkding': {'app': 'sandbox-ld'}, 'hc': {'app': 'sandbox-hc'}}
        monkeypatch.setattr(acc, 'inspect', lambda kind, ref: {'State': {'Running': True, 'StartedAt': 'unchanged'}})
        matrix = Matrix(acc, {}, prov, 'hc-image')
        matrix.deadline = float('inf')
        try:
            for target_id, name in [(matrix.linkding_id, 'sandbox-ld'),(matrix.hc_id, 'sandbox-hc')]:
                await matrix.service.enroll(TargetSpec(target_id=target_id,name=name,container_name=name,
                    probes=[{'url':'http://127.0.0.1:18093/health'}]))
            row=matrix.store.create_investigation(matrix.linkding_id, 'Completed test')
            matrix.store.update_investigation(row['investigation_id'],phase='needs_human')
            result=await matrix.case_reopen()
            assert result['history_survived'] and result['investigations']==1
            assert result['apps_alive_after_disconnect']
        finally:
            matrix.store.close()
    asyncio.run(check())
