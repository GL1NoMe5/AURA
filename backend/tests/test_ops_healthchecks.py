"""Offline independent second-app enrollment/ownership checks. No Docker or AI."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
from aura_ops.models import TargetSpec
from tools.ops.healthchecks import CONTAINER, GATEWAY, PROJECT, HealthchecksLab
from tools.ops.lab import LabError


IMAGE_ID = "sha256:" + "b" * 64


def test_setup_pulls_only_immutable_official_image_and_preserves_local_secret(tmp_path):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[3] == "version":
            return 0, '{"Version":"test","Os":"linux"}', ""
        if argv[3:5] == ["image", "inspect"]:
            return 0, json.dumps([{"Id": IMAGE_ID, "Os": "linux", "Architecture": "amd64"}]), ""
        assert argv[3:6] == ["pull", "--platform", "linux/amd64"]
        assert argv[-1].startswith("healthchecks/healthchecks@sha256:")
        return 0, "", ""
    lab = HealthchecksLab(tmp_path, "desktop-linux", runner=runner)
    first = lab.setup()
    initial_secret = (tmp_path / ".env").read_bytes()
    second = lab.setup()
    assert first["image_id"] == second["image_id"] == IMAGE_ID
    assert (tmp_path / ".env").read_bytes() == initial_secret
    assert len(initial_secret) > 48
    assert all(argv[1:3] == ["--context", "desktop-linux"] for argv in calls)
    assert all("up" not in argv and "run" not in argv and "exec" not in argv for argv in calls)
    assert initial_secret.decode().split("=", 1)[1].strip() not in json.dumps([first, second])
    assert not first["portability_verified"] and first["provider_requests"] == 0


def test_holdout_is_isolated_without_source_patching_or_implicit_permissions(tmp_path):
    lab = HealthchecksLab(tmp_path)
    compose = lab.compose()
    app = compose["services"]["healthchecks"]
    gateway = compose["services"]["gateway"]
    assert "ports" not in app
    assert app["networks"] == ["isolated"]
    assert app["mem_limit"] == app["memswap_limit"] == "512m"
    assert compose["networks"]["isolated"]["internal"] is True
    assert gateway["ports"] == ["127.0.0.1:18091:8080"]
    assert gateway["networks"] == ["ingress", "isolated"]
    assert gateway["read_only"] is True
    assert gateway["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in gateway["security_opt"]
    assert gateway["mem_limit"] == gateway["memswap_limit"] == "64m"
    assert gateway["command"] == ["/gateway.py"]
    assert gateway["volumes"] == [str((ROOT / "tools/ops/healthchecks_gateway.py").resolve()) + ":/gateway.py:ro"]
    assert app["environment"]["EMAIL_HOST"] == ""
    assert app["environment"]["REGISTRATION_OPEN"] == "False"
    assert not any("docker.sock" in mount for mount in app["volumes"] + gateway["volumes"])
    assert "environment" not in gateway and "env_file" not in gateway
    credentials = {"checks_path": "/projects/" + "b" * 8 + "-1234-1234-1234-" + "b" * 12 + "/checks/",
                   "password": "DO_NOT_EXPORT", "cookie": "sessionid=DO_NOT_EXPORT"}
    exported = lab.target_profile(credentials)
    target = TargetSpec.model_validate(exported)
    assert target.source_dir is None and target.patch_profile is None
    assert not target.policy.automatic_actions and not target.policy.auto_investigate
    assert len(target.probes) == 2 and target.probes[1].auth_scheme == "Cookie"
    assert target.probes[1].credential_env == "AURA_TARGET_HEALTHCHECKS_COOKIE"
    assert "DO_NOT_EXPORT" not in json.dumps(exported)


@pytest.mark.parametrize("owned", [True, False])
def test_stop_only_mutates_exact_owned_container_and_keeps_volume(tmp_path, owned):
    calls = []
    def row_for(name):
        return {"Id": "a" * 64 if name == CONTAINER else "b" * 64, "Name": "/" + name,
                "Config": {"Labels": {"org.aura.holdout": PROJECT if owned else "other",
                                      "com.docker.compose.project": PROJECT}}}
    def runner(argv, **kwargs):
        calls.append(argv)
        return (0, json.dumps([row_for(argv[4])]), "") if argv[3] == "inspect" else (0, "", "")
    lab = HealthchecksLab(tmp_path, runner=runner)
    if owned:
        result = lab.stop()
        assert result["volumes_preserved"] and result["gateway_stopped"]
        assert calls[0][3:] == ["inspect", CONTAINER]
        assert calls[1][3:] == ["inspect", GATEWAY]
        assert calls[2][3:] == ["stop", "--time", "10", "a" * 64]
        assert calls[3][3:] == ["stop", "--time", "10", "b" * 64]
    else:
        with pytest.raises(LabError) as error:
            lab.stop()
        assert error.value.code == "ownership_mismatch"
        assert len(calls) == 1


def test_stop_refuses_foreign_gateway_without_stopping_app(tmp_path):
    calls = []
    app_row = {"Id": "a" * 64, "Name": "/" + CONTAINER, "Config": {"Labels": {
        "org.aura.holdout": PROJECT, "com.docker.compose.project": PROJECT}}}
    foreign = {"Id": "b" * 64, "Name": "/" + GATEWAY, "Config": {"Labels": {
        "org.aura.holdout": "other", "com.docker.compose.project": PROJECT}}}
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[3] == "inspect":
            return 0, json.dumps([app_row if argv[4] == CONTAINER else foreign]), ""
        return 0, "", ""
    lab = HealthchecksLab(tmp_path, runner=runner)
    with pytest.raises(LabError) as error:
        lab.stop()
    assert error.value.code == "ownership_mismatch"
    assert [argv[3] for argv in calls] == ["inspect", "inspect"]


def test_gateway_is_fixed_destination_with_bounds_and_no_proxy_controls():
    source = (ROOT / "tools/ops/healthchecks_gateway.py").read_text(encoding="utf-8")
    assert 'TARGET_HOST = "healthchecks"' in source
    assert "TARGET_PORT = 8000" in source and "LISTEN_PORT = 8080" in source
    assert "MAX_CLIENTS = 16" in source
    assert "open_connection(TARGET_HOST, TARGET_PORT)" in source
    assert "timeout=180" in source
    for forbidden in ("argparse", "sys.argv", "os.environ", "getenv", "docker.sock",
                      "subprocess", "shutil", "proxy_pass", "socks", "HTTP_PROXY"):
        assert forbidden not in source


def test_status_missing_docker_remains_read_only_and_unverified(tmp_path):
    def runner(*args, **kwargs):
        raise LabError("docker_missing", "missing")
    result = HealthchecksLab(tmp_path, runner=runner).status()
    assert not result["docker_ready"] and not result["portability_verified"]
    assert result["provider_requests"] == 0
    assert list(tmp_path.iterdir()) == []


def test_start_exports_generic_target_after_both_http_probes_without_exposing_secrets(tmp_path, monkeypatch):
    import tools.ops.healthchecks as module
    from tools.ops.lab import write_json
    paths = []
    credentials = {"cookie": "sessionid=LOCAL_PRIVATE_VALUE", "password": "LOCAL_PRIVATE_PASSWORD",
                   "checks_path": "/projects/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/checks/"}
    row = {"Id": "a" * 64, "Image": IMAGE_ID, "Name": "/" + CONTAINER, "Config": {"Labels": {
        "org.aura.holdout": PROJECT, "com.docker.compose.project": PROJECT}}}
    gateway_row = {"Id": "c" * 64, "Image": IMAGE_ID, "Name": "/" + GATEWAY, "Config": {"Labels": {
        "org.aura.holdout": PROJECT, "com.docker.compose.project": PROJECT}}}
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[3:5] == ["image", "inspect"]:
            return 0, json.dumps([{"Id": IMAGE_ID, "Os": "linux", "Architecture": "amd64"}]), ""
        if argv[3] == "inspect":
            return 0, json.dumps([row if argv[4] == CONTAINER else gateway_row]), ""
        if argv[3] == "exec":
            assert kwargs["input_text"] and "SessionStore" in kwargs["input_text"]
            return 0, "AURA_HEALTHCHECKS=" + json.dumps(credentials), ""
        assert argv[3] == "compose" and argv[-2:] == ["healthchecks", "gateway"]
        return 0, "", ""
    def probe(url, headers, **kwargs):
        paths.append((url, headers))
        return {"status_code": 200, "error": None, "bytes": 12, "sha256": "a" * 64}
    monkeypatch.setattr(module, "download", probe)
    lab = HealthchecksLab(tmp_path, runner=runner)
    write_json(tmp_path / "image.json", {"image": lab.provenance["image"], "image_id": IMAGE_ID})
    (tmp_path / ".env").write_text("SECRET_KEY=" + "s" * 64, encoding="utf-8")
    result = lab.start()
    assert len(paths) == 2
    assert paths[0][0].endswith("/health")
    assert paths[1][1] == {"Cookie": credentials["cookie"]}
    exported = json.loads((tmp_path / "target.json").read_text())
    assert TargetSpec.model_validate(exported).target_id == "healthchecks-holdout"
    assert "LOCAL_PRIVATE" not in json.dumps(result)
    assert "LOCAL_PRIVATE" not in json.dumps(exported)
    assert not result["restart_verified"] and result["provider_requests"] == 0
    assert (tmp_path / "credentials.env").read_text().strip() == "AURA_TARGET_HEALTHCHECKS_COOKIE=" + credentials["cookie"]
    evidence = json.loads((tmp_path / "evidence/start.json").read_text())
    assert not evidence["portability_verified"] and not evidence["restart_verified"]
    assert evidence["gateway_container_id"] == "c" * 64
    assert "LOCAL_PRIVATE" not in json.dumps(evidence)


def test_start_refuses_foreign_gateway_before_mutation(tmp_path):
    from tools.ops.lab import write_json
    app_row = {"Id": "a" * 64, "Image": IMAGE_ID, "Name": "/" + CONTAINER, "Config": {"Labels": {
        "org.aura.holdout": PROJECT, "com.docker.compose.project": PROJECT}}}
    foreign = {"Id": "b" * 64, "Image": IMAGE_ID, "Name": "/" + GATEWAY, "Config": {"Labels": {
        "org.aura.holdout": "other", "com.docker.compose.project": PROJECT}}}
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[3:5] == ["image", "inspect"]:
            return 0, json.dumps([{"Id": IMAGE_ID, "Os": "linux", "Architecture": "amd64"}]), ""
        return 0, json.dumps([app_row if argv[4] == CONTAINER else foreign]), ""
    lab = HealthchecksLab(tmp_path, runner=runner)
    write_json(tmp_path / "image.json", {"image": lab.provenance["image"], "image_id": IMAGE_ID})
    with pytest.raises(LabError) as error:
        lab.start()
    assert error.value.code == "ownership_mismatch"
    assert [argv[3] for argv in calls] == ["image", "inspect", "inspect"]
    assert not (tmp_path / "compose.json").exists()


def test_smoke_reports_healthy_without_credentials(tmp_path, monkeypatch, capsys):
    import tools.ops.check_healthchecks as smoke
    from tools.ops.lab import write_json
    lab = HealthchecksLab(tmp_path)
    credentials = {"cookie": "sessionid=LOCAL_PRIVATE_VALUE", "password": "LOCAL_PRIVATE_PASSWORD",
                   "checks_path": "/projects/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/checks/"}
    write_json(tmp_path / "target.json", lab.target_profile(credentials))
    write_json(tmp_path / "credentials.json", credentials)
    class StubLab:
        def __init__(self, context):
            self.lab, self.context = tmp_path, context
        def inspect_owned(self, name=CONTAINER):
            return {"Id": ("a" if name == CONTAINER else "c") * 64, "Image": IMAGE_ID,
                    "State": {"Running": True}}
    monkeypatch.setattr(smoke, "HealthchecksLab", StubLab)
    monkeypatch.setattr(smoke, "download",
                        lambda url, headers, **kwargs: {"status_code": 200, "error": None,
                                                        "bytes": 12, "sha256": "a" * 64})
    assert smoke.main(["--context", "desktop-linux"]) == 0
    output = capsys.readouterr().out
    assert '"healthy": true' in output
    assert "LOCAL_PRIVATE" not in output
    assert "LOCAL_PRIVATE" not in (tmp_path / "evidence/smoke.json").read_text()
