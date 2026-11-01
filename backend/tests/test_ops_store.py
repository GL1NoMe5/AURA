"""Operations persistence: durable history, one-shot transitions, artifact trust."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from aura_ops.models import OpsError
from aura_ops.store import MAX_ARTIFACT_BYTES, MAX_JSON_BYTES, OpsStore
import aura_ops.store as store_module


def target(name="linkding"):
    return {"target_id": name, "name": name.title(), "container_name": name,
            "docker_context": "default", "monitoring": True,
            "probes": [{"url": "http://127.0.0.1:9090/"}],
            "policy": {"automatic_actions": [], "auto_investigate": False}}


@pytest.fixture
def store(tmp_path):
    instance = OpsStore(tmp_path)
    yield instance
    instance.close()


def assert_error(code, callable_):
    with pytest.raises(OpsError) as caught:
        callable_()
    assert caught.value.code == code


def test_records_and_sequence_survive_close_and_reopen(tmp_path):
    first = OpsStore(tmp_path)
    registered = first.put_target(target())
    run = first.create_investigation("linkding", "Why did file downloads fail?")
    first.update_investigation(run["investigation_id"], phase="investigating",
                               evidence=[{"source": "docker", "memory_bytes": 123}])
    proposal = first.put_proposal({"investigation_id": run["investigation_id"],
                                  "target_id": "linkding", "action": "restart_container"})
    event = first.append_event("investigation.started", {"phase": "investigating"},
                               "linkding", run["investigation_id"])
    first.put_setting("restart.linkding", {"last_at": 123.5})
    first.close()
    second = OpsStore(tmp_path)
    try:
        assert second.get_target("linkding") == registered
        assert second.get_investigation(run["investigation_id"])["phase"] == "investigating"
        assert second.get_investigation(run["investigation_id"])["evidence"][0]["memory_bytes"] == 123
        assert second.get_proposal(proposal["proposal_id"])["status"] == "proposed"
        assert second.get_setting("restart.linkding") == {"last_at": 123.5}
        assert second.events_since(0) == [event]
        assert second.append_event("startup", {})["seq"] == event["seq"] + 1
    finally:
        second.close()


def test_target_updates_preserve_metadata_and_cap_only_new_targets(store):
    first = store.put_target({**target(), "registered_identity": {"id": "container-a"}})
    second = store.put_target({**target(), "name": "Bookmarks", "created_at": "fake"})
    assert second["name"] == "Bookmarks"
    assert second["created_at"] == first["created_at"]
    assert second["registered_identity"] == first["registered_identity"]
    store.put_target(target("healthchecks"))
    store.put_target(target("api"))
    assert_error("target_limit", lambda: store.put_target(target("fourth")))
    store.put_target({**target("api"), "name": "Changed"})
    assert len(store.list_targets()) == 3
    store.delete_target("healthchecks")
    store.put_target(target("fourth"))
    assert store.get_target("healthchecks") is None


def test_target_cap_is_atomic_across_store_instances(tmp_path):
    stores = [OpsStore(tmp_path) for _ in range(4)]
    barrier = threading.Barrier(4)

    def add(i):
        barrier.wait()
        try:
            stores[i].put_target(target(f"app-{i}"))
            return True
        except OpsError as error:
            assert error.code == "target_limit"
            return False

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            assert sum(executor.map(add, range(4))) == 3
        assert len(stores[0].list_targets()) == 3
    finally:
        for instance in stores:
            instance.close()


def test_investigations_have_stable_identity_and_filtered_history(store):
    store.put_target(target())
    store.put_target(target("api"))
    old = store.create_investigation("linkding", "old")
    other = store.create_investigation("api", "other", trigger="monitor")
    new = store.create_investigation("linkding", "new")
    assert [run["question"] for run in store.list_investigations("linkding")] == ["new", "old"]
    assert store.list_investigations(limit=1)[0]["investigation_id"] == new["investigation_id"]
    assert_error("immutable_record", lambda: store.update_investigation(
        old["investigation_id"], target_id="api"))
    assert store.get_investigation(old["investigation_id"])["target_id"] == "linkding"
    store.delete_target("api")
    assert store.get_investigation(other["investigation_id"])["question"] == "other"
    assert_error("target_not_found", lambda: store.create_investigation("missing", "why"))
    assert_error("investigation_not_found", lambda: store.update_investigation("missing", phase="done"))


def test_event_pruning_keeps_global_sequence_and_cursor_order(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_EVENTS", 3)
    instance = OpsStore(tmp_path)
    for i in range(7):
        event = instance.append_event("sample", {"i": i})
        assert event["seq"] == i + 1
    assert [e["seq"] for e in instance.events_since(0)] == [5, 6, 7]
    assert [e["seq"] for e in instance.events_since(5, limit=1)] == [6]
    assert instance.latest_seq() == 7
    instance.close()
    reopened = OpsStore(tmp_path)
    try:
        assert reopened.append_event("sample", {})["seq"] == 8
        assert [e["seq"] for e in reopened.events_since(0)] == [6, 7, 8]
    finally:
        reopened.close()


def test_proposal_transition_is_one_shot_across_threads_and_connections(tmp_path):
    stores = [OpsStore(tmp_path), OpsStore(tmp_path)]
    proposal = stores[0].put_proposal({"target_id": "linkding", "proposal_hash": "a" * 64})
    barrier = threading.Barrier(8)

    def claim(i):
        barrier.wait()
        return stores[i % 2].transition_proposal(
            proposal["proposal_id"], "proposed", "executing", claimant=i)

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(claim, range(8)))
        assert sum(result is not None for result in results) == 1
        assert stores[0].get_proposal(proposal["proposal_id"])["status"] == "executing"
        assert stores[0].transition_proposal(proposal["proposal_id"], "proposed", "executing") is None
        assert stores[0].transition_proposal(proposal["proposal_id"], "executing", "verified")["status"] == "verified"
    finally:
        for instance in stores:
            instance.close()


def test_proposal_filter_update_and_identity_protection(store):
    one = store.put_proposal({"investigation_id": "first", "target_id": "linkding"})
    two = store.put_proposal({"investigation_id": "second", "target_id": "api"})
    assert store.list_proposals("first") == [one]
    assert {p["proposal_id"] for p in store.list_proposals()} == {one["proposal_id"], two["proposal_id"]}
    updated = store.update_proposal(one["proposal_id"], result={"healthy": True})
    assert updated["result"] == {"healthy": True}
    assert updated["created_at"] == one["created_at"]
    assert_error("immutable_record", lambda: store.update_proposal(one["proposal_id"], target_id="api"))
    assert_error("proposal_not_found", lambda: store.update_proposal("missing", status="done"))


def test_mutable_input_and_return_values_do_not_change_persisted_records(store):
    spec = target()
    registered = store.put_target(spec)
    spec["policy"]["automatic_actions"].append("restart_container")
    registered["policy"]["automatic_actions"].append("restore_config_snapshot")
    assert store.get_target("linkding")["policy"]["automatic_actions"] == []
    payload = {"finding": ["safe"]}
    event = store.append_event("sample", payload)
    payload["finding"].append("mutated")
    event["payload"]["finding"].append("also-mutated")
    assert store.events_since(0)[0]["payload"] == {"finding": ["safe"]}


def test_artifact_roundtrip_survives_reopen_and_checks_content(tmp_path):
    instance = OpsStore(tmp_path)
    content = b"diff --git a/app.py b/app.py\n"
    artifact = instance.save_artifact("fix.patch", content, "text/x-diff")
    assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
    assert "path" not in artifact
    instance.close()
    reopened = OpsStore(tmp_path)
    try:
        loaded = reopened.get_artifact(artifact["artifact_id"])
        assert Path(loaded["path"]).read_bytes() == content
        assert Path(loaded["path"]).parent == (tmp_path / "artifacts").resolve()
        Path(loaded["path"]).write_bytes(b"X" + content[1:])
        assert_error("artifact_corrupt", lambda: reopened.get_artifact(artifact["artifact_id"]))
    finally:
        reopened.close()


def test_missing_truncated_and_unknown_artifacts_are_not_returned(store):
    one = store.save_artifact("evidence.json", b"{}", "application/json")
    path = Path(store.get_artifact(one["artifact_id"])["path"])
    path.write_bytes(b"{")
    assert_error("artifact_corrupt", lambda: store.get_artifact(one["artifact_id"]))
    path.unlink()
    assert_error("artifact_missing", lambda: store.get_artifact(one["artifact_id"]))
    assert store.get_artifact("0" * 32) is None


@pytest.mark.parametrize("name", ["../x", "..", ".", "/x", "C:\\x", "a/b", "a\\b", "x:stream", "x\n.json", " x"])
def test_artifact_display_name_cannot_select_disk_path(store, name):
    assert_error("invalid_artifact_name", lambda: store.save_artifact(name, b"content"))
    assert not list(store.artifacts_dir.iterdir())


def test_failed_artifact_metadata_commit_cleans_owned_files(store):
    store._db.execute("CREATE TRIGGER fail_artifact BEFORE INSERT ON artifacts "
                      "BEGIN SELECT RAISE(FAIL, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.save_artifact("test.txt", b"test", "text/plain")
    assert list(store.artifacts_dir.iterdir()) == []
    assert store._db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_invalid_json_and_limits_leave_existing_records_unchanged(store):
    stored = store.put_target(target())
    assert_error("record_too_large", lambda: store.put_target({**target(), "extra": "a" * MAX_JSON_BYTES}))
    assert store.get_target("linkding") == stored
    assert_error("invalid_record", lambda: store.append_event("sample", {"value": float("nan")}))
    assert store.latest_seq() == 0
    assert_error("artifact_too_large", lambda: store.save_artifact("large.bin", b"x" * (MAX_ARTIFACT_BYTES + 1)))
    assert_error("invalid_media_type", lambda: store.save_artifact("test.txt", b"x", "text/plain\r\nX: y"))
    assert_error("invalid_artifact_id", lambda: store.get_artifact("../outside"))
    assert_error("invalid_identifier", lambda: store.get_target("x' OR 1=1"))
    assert_error("invalid_sequence", lambda: store.events_since(-1))
    assert_error("invalid_limit", lambda: store.list_investigations(limit=0))


def test_settings_are_json_not_shared_objects_and_close_is_idempotent(store):
    value = {"image": "sha256:abc", "ports": [9090]}
    store.put_setting("snapshot.linkding", value)
    value["ports"].append(9091)
    assert store.get_setting("snapshot.linkding")["ports"] == [9090]
    assert store.get_setting("missing", {"default": True}) == {"default": True}
    store.close()
    store.close()
    assert_error("store_closed", store.list_targets)
