"""Offline evaluator safety and honest evidence classification; no Docker/LLM."""
import io
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tarfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
from tools.ops.lab import (  # noqa: E402
    CONTAINER, GATEWAY, IMAGES, PROJECT, SOURCES, TEST_PATHS, DockerLab, LabError, check_manifest,
    classify_result, download, file_sha, manifest_for, requirements_sha, safe_extract, write_json,
)


def archive_at(path, entries):
    with tarfile.open(path, "w:gz") as output:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            if kind == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                output.addfile(member)
            else:
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))


def test_archive_provenance_and_extraction(tmp_path):
    archive = tmp_path / "source.tgz"
    destination = tmp_path / "source"
    archive_at(archive, [("commit/app/a.py", b"x = 1\n", "file")])
    safe_extract(archive, destination, "commit")
    manifest = manifest_for(destination, "a" * 40)
    check_manifest(manifest)
    assert manifest["files"].keys() == {"app/a.py"}
    (destination / "app/a.py").write_text("x = 2\n")
    with pytest.raises(LabError, match="differs"):
        check_manifest(manifest)


@pytest.mark.parametrize("name,kind", [
    ("commit/../../outside", "file"), ("/absolute", "file"),
    ("other/a.py", "file"), ("commit/C:foo", "file"),
    ("commit/a\\..\\x", "file"), ("commit/link", "link"),
])
def test_archive_rejected_before_any_write(tmp_path, name, kind):
    archive = tmp_path / "source.tgz"
    destination = tmp_path / "source"
    archive_at(archive, [("commit/valid.py", b"ok", "file"), (name, b"no", kind)])
    with pytest.raises(LabError):
        safe_extract(archive, destination, "commit")
    assert not destination.exists()


def owned_row():
    return {"Id": "a" * 64, "Name": "/" + CONTAINER,
            "Config": {"Labels": {"org.aura.lab": PROJECT,
                                 "com.docker.compose.project": PROJECT}}}


def test_stop_uses_resolved_owned_id_and_explicit_context(tmp_path):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if "inspect" in argv:
            row = owned_row()
            if argv[-1] == GATEWAY:
                row.update(Id="b" * 64, Name="/" + GATEWAY)
            return 0, json.dumps([row]), ""
        return 0, "", ""
    DockerLab(tmp_path, "desktop-linux", runner).stop()
    assert len(calls) == 4
    assert all(argv[1:3] == ["--context", "desktop-linux"] for argv in calls)
    assert calls[2][3:] == ["stop", "--time", "10", "a" * 64]
    assert calls[3][3:] == ["stop", "--time", "10", "b" * 64]


def test_cannot_stop_foreign_container(tmp_path):
    row = owned_row()
    row["Config"]["Labels"]["org.aura.lab"] = "someone-else"
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return 0, json.dumps([row]), ""
    with pytest.raises(LabError, match="not owned"):
        DockerLab(tmp_path, runner=runner).stop()
    assert len(calls) == 1


def test_status_is_read_only_and_missing_docker_not_success(tmp_path):
    def runner(argv, **kwargs):
        raise LabError("docker_missing", "missing")
    before = list(tmp_path.iterdir())
    status = DockerLab(tmp_path, runner=runner).status()
    assert not status["docker_ready"]
    assert not status["runtime_reproduction_verified"]
    assert status["provider_requests"] == 0
    assert list(tmp_path.iterdir()) == before


def sample(oom):
    return {"cgroup": {"memory.events": f"low 0\nhigh 0\nmax 12\noom 1\noom_kill {oom}\n"}}


def test_sigkill_or_http_failure_alone_is_not_memory_proof():
    outcome = classify_result(sample(2), sample(2), [{"complete": False}],
                              "worker died, killed by signal 9", {"status_code": 200})
    assert outcome["worker_sigkill_log_seen"]
    assert not outcome["reproduced_memory_failure"]
    assert not outcome["verified_complete_transfer"]


def test_fresh_cgroup_oom_delta_and_transfer_failure_prove_reproduction():
    outcome = classify_result(sample(2), sample(3), [{"complete": False}], "", {"status_code": 200})
    assert outcome["reproduced_memory_failure"]
    assert outcome["oom_kill_delta"] == 1


def test_reference_needs_both_integrity_checked_transfers_and_no_new_oom():
    good = [{"complete": True}, {"complete": True}]
    assert classify_result(sample(3), sample(3), good, "", {"status_code": 200, "error": None})["verified_complete_transfer"]
    assert not classify_result(sample(3), sample(4), good, "", {"status_code": 200})["verified_complete_transfer"]
    assert not classify_result(sample(3), {}, good, "", {"status_code": 200})["verified_complete_transfer"]
    assert not classify_result(sample(3), sample(3), good[:1], "", {"status_code": 200})["verified_complete_transfer"]


def test_compose_limits_and_isolation(tmp_path):
    lab = DockerLab(tmp_path)
    compose = lab.compose("bad", 256)
    service = compose["services"]["linkding"]
    assert "ports" not in service
    assert service["networks"] == ["isolated"]
    gateway = compose["services"]["gateway"]
    assert gateway["ports"] == ["127.0.0.1:18090:8080"]
    assert gateway["networks"] == ["ingress", "isolated"]
    assert gateway["read_only"] and gateway["cap_drop"] == ["ALL"]
    assert gateway["volumes"][0].endswith(":/gateway.py:ro")
    assert service["mem_limit"] == service["memswap_limit"] == "256m"
    assert compose["networks"]["isolated"]["internal"] is True
    assert service["restart"] == "no"
    assert not any("docker.sock" in value for value in service["volumes"])
    with pytest.raises(LabError):
        lab.compose("bad", 4096)
    with pytest.raises(LabError):
        lab.compose("untrusted-image", 256)


def test_http_transfer_hash_size_bound_and_no_redirect():
    received = []
    content = b"A" * (130 * 1024)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        good = download(origin + "/ok", {"Authorization": "Token LOCAL_TEST_VALUE"}, len(content))
        assert good["bytes"] == len(content)
        assert good["sha256"] == hashlib.sha256(content).hexdigest()
        assert good["error"] is None
        assert "LOCAL_TEST_VALUE" not in json.dumps(good)
        truncated = download(origin + "/bounded", {}, 100)
        assert truncated["error"] == "ValueError"
        redirect = download(origin + "/redirect", {}, 100)
        assert redirect["status_code"] == 302 and redirect["error"] == "http_error"
        assert "/must-not-follow" not in received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def prepared_build(tmp_path):
    config = json.loads(SOURCES.read_text(encoding="utf-8"))["linkding"]
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("def health(): return True\n", encoding="utf-8")
    write_json(tmp_path / "source-manifest.json", manifest_for(source, config["revision"]))
    images = {"bad": "sha256:" + "b" * 64, "reference": "sha256:" + "c" * 64,
              "test": "sha256:" + "d" * 64}
    write_json(tmp_path / "images.json", {"revision": config["revision"], "images": images,
        "source_manifest_sha256": file_sha(tmp_path / "source-manifest.json"),
        "requirements_sha256": config["test_profile"]["requirements_sha256"]})
    return images


def test_target_export_binds_images_and_both_complete_downloads_without_credentials(tmp_path):
    from aura_ops.models import TargetSpec
    images = prepared_build(tmp_path)
    credentials = {"token": "DO_NOT_EXPORT_TOKEN", "sessionid": "DO_NOT_EXPORT_SESSION",
        "session_cookie_name": "ld_sessionid", "bookmark_id": 4, "asset_id": 9,
        "expected_bytes": 500 * 1024 * 1024, "expected_sha256": "e" * 64}
    lab = DockerLab(tmp_path)
    profile = lab.target_profile({"Image": images["bad"]}, credentials, tmp_path / "compose.json")
    model = TargetSpec.model_validate(profile)
    assert model.source_image_id == images["bad"]
    assert model.source_manifest_sha256 == file_sha(tmp_path / "source-manifest.json")
    assert model.deployment_root == "/etc/linkding"
    assert model.deployment_launch.entrypoint == ["/etc/linkding/.venv/bin/uwsgi"]
    assert model.deployment_launch.command == ["--http", "0.0.0.0:9090", "/etc/linkding/uwsgi.ini"]
    assert model.patch_profile.image == images["test"]
    assert model.patch_profile.test_paths == TEST_PATHS
    assert len(model.recovery_checks) == 2
    assert all(check.expected_bytes == 500 * 1024 * 1024 and check.expected_sha256 == "e" * 64
               for check in model.recovery_checks)
    assert model.recovery_checks[0].url.endswith("/api/bookmarks/4/assets/9/download/")
    assert model.recovery_checks[1].url.endswith("/assets/9")
    assert model.recovery_checks[1].auth_scheme == "Cookie"
    assert model.recovery_checks[1].credential_env == "AURA_LAB_SESSION_COOKIE"
    assert not model.policy.automatic_actions and not model.policy.auto_investigate
    text = json.dumps(profile)
    assert "DO_NOT_EXPORT" not in text
    assert "private-reference" not in text
    assert images["reference"] not in text


def test_export_rejects_reference_or_changed_build_source(tmp_path):
    images = prepared_build(tmp_path)
    lab = DockerLab(tmp_path)
    with pytest.raises(LabError) as wrong_image:
        lab.target_profile({"Image": images["reference"]}, {}, tmp_path / "compose.json")
    assert wrong_image.value.code == "source_image_mismatch"
    (tmp_path / "source/app.py").write_text("def health(): return False\n", encoding="utf-8")
    with pytest.raises(LabError) as wrong_source:
        lab.target_profile({"Image": images["bad"]}, {}, tmp_path / "compose.json")
    assert wrong_source.value.code == "source_modified"


def test_start_rejects_retagged_image_before_container_mutation(tmp_path):
    prepared_build(tmp_path)
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv[-3:] == ["image", "inspect", IMAGES["bad"]]
        return 0, json.dumps([{"Id": "sha256:" + "f" * 64, "Os": "linux", "Architecture": "amd64"}]), ""
    with pytest.raises(LabError) as error:
        DockerLab(tmp_path, runner=runner).start()
    assert error.value.code == "image_identity_changed"
    assert len(calls) == 1
    assert not (tmp_path / "compose.json").exists()
    assert not (tmp_path / "credentials.json").exists()


def test_test_requirements_have_pinned_wheel_hashes_and_no_browser_dependencies():
    config = json.loads(SOURCES.read_text(encoding="utf-8"))["linkding"]["test_profile"]
    requirements = ROOT / "resources/ops/test-requirements.txt"
    assert requirements_sha(requirements) == config["requirements_sha256"]
    lines = [line for line in requirements.read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    assert {line.split("==")[0] for line in lines} == set(config["packages"])
    assert all(" --hash=sha256:" in line for line in lines)
    assert not {"playwright", "django", "requests", "uwsgi"} & set(config["packages"])
    assert {"pytest", "pytest-django", "pytest-xdist"} <= set(config["packages"])


def test_test_requirement_hash_is_independent_of_windows_checkout_newlines(tmp_path):
    lf, crlf = tmp_path / "lf.txt", tmp_path / "crlf.txt"
    lf.write_bytes(b"pytest==9.0.3 --hash=sha256:abc\n")
    crlf.write_bytes(b"pytest==9.0.3 --hash=sha256:abc\r\n")
    assert requirements_sha(lf) == requirements_sha(crlf)


@pytest.mark.parametrize("kind", ["memory_limit", "port_mismatch"])
def test_config_fault_uses_separate_override_and_reset_preserves_baseline(tmp_path, kind):
    calls = []
    row = owned_row()
    row["Image"] = "sha256:" + "b" * 64
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[3] == "inspect":
            return 0, json.dumps([row]), ""
        assert argv[3] == "compose"
        row["Id"] = "c" * 64
        return 0, "", ""
    lab = DockerLab(tmp_path, runner=runner)
    base = tmp_path / "compose.json"
    compose = lab.compose("bad", 256)
    compose["services"]["linkding"]["image"] = row["Image"]
    write_json(base, compose)
    original = base.read_bytes()
    write_json(tmp_path / "active.json", {"image_id": row["Image"], "container_id": row["Id"],
        "memory_mib": 256, "variant": "bad", "compose_sha256": file_sha(base)})
    result = lab.fault(kind)
    assert result["operator_induced_config_change"] and not result["runtime_fault_verified"]
    assert base.read_bytes() == original
    override = json.loads((tmp_path / "fault-override.json").read_text())["services"]["linkding"]
    assert override["entrypoint"] == ["/etc/linkding/.venv/bin/uwsgi"]
    if kind == "memory_limit":
        assert override["mem_limit"] == override["memswap_limit"] == "96m"
    else:
        assert override["environment"]["LD_SERVER_PORT"] == "9091"
        assert "ports" not in override  # Published 18090 -> 9090 remains unchanged.
    dispatch = next(argv for argv in calls if "compose" in argv)
    assert dispatch.count("--file") == 2 and "--no-deps" in dispatch and "--force-recreate" in dispatch
    with pytest.raises(LabError) as error:
        lab.reproduce()
    assert error.value.code == "config_fault_active"
    calls.clear()
    reset = lab.reset_fault()
    assert reset["baseline_reapplied"] and not reset["recovery_verified"]
    assert base.read_bytes() == original
    dispatch = next(argv for argv in calls if "compose" in argv)
    assert dispatch.count("--file") == 1
    assert json.loads((tmp_path / "active.json").read_text())["container_id"] == "c" * 64


def test_config_fault_refuses_modified_baseline_before_any_docker_call(tmp_path):
    base = tmp_path / "compose.json"
    base.write_text("{}", encoding="utf-8")
    write_json(tmp_path / "active.json", {"compose_sha256": file_sha(base)})
    base.write_text('{"changed":true}', encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("No Docker command allowed for stale baseline")
    with pytest.raises(LabError) as error:
        DockerLab(tmp_path, runner=forbidden).fault("port_mismatch")
    assert error.value.code == "baseline_config_changed"
