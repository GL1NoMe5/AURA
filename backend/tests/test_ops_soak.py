"""Offline tests for the soak runner: gate math, WS ordering, cancellation,
ownership refusal. No Docker, no network, no provider. Docker boundaries
are faked; production validation helpers are real.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.ops.acceptance import RunnerError
import tools.ops.soak as soak
from tools.ops.soak import (
    SoakAcceptance, WsTracker, coverage_gate, evaluate_gates, gap_gate,
    identity_changes, provision_linkding, rss_gate, slope_per_minute,
)


RUN = "soak-test012345"


def test_coverage_gate_boundaries():
    assert coverage_gate(400)["passed"] is True
    assert coverage_gate(380)["passed"] is True
    assert coverage_gate(380)["ratio"] == pytest.approx(0.95)
    assert coverage_gate(379)["passed"] is False
    assert coverage_gate(0)["passed"] is False


def test_gap_gate_flags_only_unexplained_gaps():
    assert gap_gate([0.0, 3.0, 6.0, 9.0])["passed"] is True
    assert gap_gate([]) == {"max_gap_seconds": 0.0, "passed": True, "violations": []}
    result = gap_gate([0.0, 3.0, 25.0])
    assert result["passed"] is False
    assert result["violations"] == [22.0]


def test_slope_per_minute_linear():
    assert slope_per_minute([(0.0, 10.0), (60.0, 20.0)]) == pytest.approx(10.0)
    assert slope_per_minute([(0.0, 5.0)]) is None
    assert slope_per_minute([(3.0, 5.0), (3.0, 7.0)]) == 0.0


def test_benchmark_reads_real_docker_oom_and_restart_fields():
    acc = SimpleNamespace(inspect=lambda kind, name: {
        'Id': 'c', 'Image': 'i', 'RestartCount': 2,
        'State': {'Running': False, 'OOMKilled': True},
        'Config': {}, 'HostConfig': {}})
    measured = soak.app_config(acc, 'owned-app')
    assert measured['oom_killed'] is True
    assert measured['restart_count'] == 2


def rss_samples(first_rss, last_rss, n_edge=100, missing=()):
    samples = []
    for tick in range(400):
        offset = tick * 3.0
        rss = first_rss if offset < 300 else last_rss if offset >= 900 else (first_rss + last_rss) // 2
        samples.append({"offset": offset, "rss": rss, "handles": 200 + tick % 7,
                        "full": tick not in missing})
    return samples


def test_rss_gate_pass_fail_and_missing():
    base = 200 * 1024 * 1024
    passed = rss_gate(rss_samples(base, base + 10 * 1024 * 1024))
    assert passed["status"] == "pass"
    assert passed["drift_bytes"] == 10 * 1024 * 1024
    assert passed["min_bytes"] <= passed["first_median_bytes"] <= passed["max_bytes"]
    assert passed["slope_bytes_per_minute"] is not None
    assert passed["handles_first_median"] is not None
    failed = rss_gate(rss_samples(base, base + 64 * 1024 * 1024))
    assert failed["status"] == "fail"
    assert failed["drift_bytes"] == 64 * 1024 * 1024


def test_rss_gate_blocks_on_unavailable_metrics():
    assert rss_gate([])["status"] == "blocked"
    sparse = rss_samples(100, 100, missing=set(range(350)))
    blocked = rss_gate(sparse)
    assert blocked["status"] == "blocked"
    assert blocked["missing"] == 350


def test_ws_tracker_strict_increase_with_snapshot_repeat():
    tracker = WsTracker()
    tracker.connect()
    tracker.snapshot(0, 100)
    tracker.event(0, 101)
    tracker.event(0, 105)
    tracker.connect()
    tracker.snapshot(1, 105)
    tracker.event(1, 106)
    verdict = tracker.verdict()
    assert verdict["passed"] is True
    assert verdict == {"connections": 2, "snapshots": 2, "violations": [], "passed": True}


def test_ws_tracker_rejects_stale_snapshot_and_disorder():
    tracker = WsTracker()
    tracker.connect()
    tracker.snapshot(0, 100)
    tracker.event(0, 110)
    tracker.connect()
    tracker.snapshot(1, 105)
    assert any("stale" in v for v in tracker.verdict()["violations"])
    tracker2 = WsTracker()
    tracker2.connect()
    tracker2.snapshot(0, 50)
    tracker2.event(0, 50)
    assert any("non-increasing" in v for v in tracker2.verdict()["violations"])
    tracker3 = WsTracker()
    tracker3.connect()
    assert tracker3.verdict()["passed"] is False


def test_identity_changes_detects_replacement_only():
    stable = [{"target_id": "a", "container_id": "c", "image_id": "i", "started_at": "t1"}] * 3
    assert identity_changes(stable) == []
    changed = stable + [{"target_id": "a", "container_id": "c", "image_id": "i", "started_at": "t2"}]
    result = identity_changes(changed)
    assert len(result) == 1 and result[0]["target_id"] == "a"


def test_run_window_records_overruns_without_pileup(tmp_path, monkeypatch):
    async def check():
        import tools.ops.soak as soak_module
        monkeypatch.setattr(soak_module, "TICK_SECONDS", 0.5)
        sampler = soak.Sampler.__new__(soak.Sampler)
        sampler.overruns = 0
        sampler.interference = []

        async def slow_sample(tick, offset):
            await asyncio.sleep(1.2)
            return {"t": "t", "tick": tick, "offset": offset}

        sampler.sample = slow_sample
        samples = await sampler.run_window(str(tmp_path / "s.jsonl"), 2.0)
        assert any(s.get("skipped") for s in samples)
        assert sampler.overruns >= 1
        assert [s["tick"] for s in samples] == sorted(s["tick"] for s in samples)
    asyncio.run(check())


def test_run_window_cancellation_closes_file(tmp_path, monkeypatch):
    async def check():
        import tools.ops.soak as soak_module
        monkeypatch.setattr(soak_module, "TICK_SECONDS", 0.1)
        sampler = soak.Sampler.__new__(soak.Sampler)
        sampler.overruns = 0
        sampler.interference = []

        async def slow_sample(tick, offset):
            await asyncio.sleep(5)
            return {"t": "t", "tick": tick, "offset": offset}

        sampler.sample = slow_sample
        task = asyncio.create_task(sampler.run_window(str(tmp_path / "s.jsonl"), 30.0))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / "s.jsonl").is_file()
    asyncio.run(check())


def test_provision_refuses_foreign_container(tmp_path):
    owned = {"org.aura.accept": RUN}

    def fake(argv, timeout=30, input_text=None):
        if argv[3:5] == ["network", "create"]:
            return 0, "n1\n", ""
        if argv[3:5] == ["volume", "create"]:
            return 0, "v1\n", ""
        if argv[3:5] == ["container", "create"]:
            return 0, "d" * 64 + "\n", ""
        if argv[3:6] == ["container", "start", "--attach"]:
            fused = '{"db": "1"}'
            return 0, f"ACCEPT_BEFORE={fused}\nACCEPT_AFTER={json.dumps({'source': {'db': '1'}, 'dest': {'db': '1'}})}", ""
        if argv[3:5] == ["container", "inspect"]:
            return 0, json.dumps([{"Config": {"Labels": owned}, "State": {"ExitCode": 0}}]), ""
        if argv[3:5] == ["container", "rm"]:
            return 0, "", ""
        if argv[3] == "compose":
            return 0, "", ""
        raise AssertionError(argv)

    acc = SoakAcceptance(RUN, cmd=fake)
    acc.dir = tmp_path / "run"

    def fake_foreign(argv, timeout=30, input_text=None):
        if argv[3:5] == ["container", "inspect"] and not argv[-1].startswith("d"):
            return 0, json.dumps([{"Id": "f" * 64, "Config": {"Labels": {"org.aura.accept": "other"}}}]), ""
        return fake(argv, timeout, input_text)

    acc.cmd = fake_foreign
    with pytest.raises(RunnerError, match="ownership mismatch"):
        provision_linkding(acc, 18093)


def test_provision_linkding_success_shape(tmp_path):
    owned = {"org.aura.accept": RUN}

    def fake(argv, timeout=30, input_text=None):
        if argv[3:5] == ["network", "create"]:
            return 0, "n1\n", ""
        if argv[3:5] == ["volume", "create"]:
            return 0, "v1\n", ""
        if argv[3:5] == ["container", "create"]:
            return 0, "d" * 64 + "\n", ""
        if argv[3:6] == ["container", "start", "--attach"]:
            return 0, 'ACCEPT_BEFORE={"db": "1"}\nACCEPT_AFTER={"source": {"db": "1"}, "dest": {"db": "1"}}', ""
        if argv[3:5] == ["container", "inspect"]:
            return 0, json.dumps([{"Id": "e" * 64, "Config": {"Labels": owned},
                                   "State": {"ExitCode": 0}}]), ""
        if argv[3:5] == ["container", "rm"]:
            return 0, "", ""
        if argv[3] == "compose":
            return 0, "", ""
        raise AssertionError(argv)

    acc = SoakAcceptance(RUN, cmd=fake)
    acc.dir = tmp_path / "run"
    prov = provision_linkding(acc, 18093)
    assert prov["port"] == 18093
    assert (tmp_path / "run" / "linkding-compose.json").is_file()
    assert (tmp_path / "run" / "fault-mem64.json").is_file()
    assert prov["copy"]["attempts"] == 1
    assert acc.dir == tmp_path / "run"


def healthy_sample(tick, offset, rss=200 * 1024 * 1024):
    return {"t": "t", "tick": tick, "offset": offset, "health_ok": True,
            "status": {"seq": 1, "targets": {
                "a": {"healthy": True, "monitoring": True, "container_id": "c",
                      "image_id": "i", "started_at": "t"}}},
            "procs": {"total_rss_bytes": rss, "full": True,
                      "per_pid": [{"pid": 1, "rss_bytes": rss, "handles": 200, "threads": 3}]}}


def test_evaluate_gates_all_pass_and_blocked():
    samples = [healthy_sample(tick, tick * 3.0) for tick in range(401)]
    watch = SimpleNamespace(tracker=WsTracker(), connections=[
        {"index": 0, "snapshot_seq": 1, "clean_close": True, "investigations": ["i"], "proposals": []},
        {"index": 1, "snapshot_seq": 1, "clean_close": True, "investigations": ["i"], "proposals": []},
        {"index": 2, "snapshot_seq": 1, "clean_close": True, "investigations": ["i"], "proposals": []}])
    for index in range(3):
        watch.tracker.connect()
        watch.tracker.snapshot(index, 1)
    gates = evaluate_gates(samples, watch, [], {"clean": True, "healthy": True})
    assert all(g["status"] == "pass" for g in gates.values()), gates
    blocked = evaluate_gates(samples, watch, [{"kind": "new_investigation", "detail": "x"}],
                             {"clean": True, "healthy": True})
    assert blocked["health"]["status"] == "blocked"
    assert blocked["health"]["interference"][0]["kind"] == "new_investigation"
    samples[-1]['status']['targets'] = {}
    missing = evaluate_gates(samples, watch, [], {"clean": True, "healthy": True}, expected_targets=['a'])
    assert missing['health']['status'] == 'fail'
    assert 400 in missing['health']['unhealthy_ticks']


def test_default_main_is_readonly(tmp_path, monkeypatch, capsys):
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

    def fake(argv, timeout=30, input_text=None):
        calls.append(argv)
        assert argv[3] not in ("create", "up", "exec", "rm", "start", "stop", "restart", "run", "build")
        if argv[3] == "version":
            return 0, '{"Version":"29.0","Os":"linux"}', ""
        if argv[3:5] == ["image", "inspect"]:
            return 0, json.dumps([{"Id": argv[-1]}]), ""
        if argv[3] == "ps":
            return 0, "", ""
        if argv[3:5] == ["container", "inspect"]:
            return 0, json.dumps([{"Id": "a" * 64, "Image": "sha256:" + "b" * 64,
                                   "State": {"StartedAt": "t"}, "Config": {"Labels": {}}}]), ""
        raise AssertionError(argv)

    import tools.ops.soak as soak_module
    monkeypatch.setattr(soak_module, "ROOT", tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("tools.ops.acceptance.execute", fake)
    monkeypatch.setattr("tools.ops.acceptance.port_occupied", lambda port: False)
    monkeypatch.setattr("tools.ops.acceptance.Acceptance.backend_health", lambda self: 200)
    assert soak_module.main([]) == 0
    assert calls
    assert all(argv[1:3] == ["--context", "desktop-linux"] for argv in calls)
