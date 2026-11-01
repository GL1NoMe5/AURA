"""Offline focused tests for the OPS-FINAL-ACCEPT-001 harness.

No Docker, no network, no provider calls. Only pure helpers, the fail-closed
runner boundary (with a stubbed command layer) and the explicit
backend-health URL injection are exercised here.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ops import acceptance as acceptance_module
from tools.ops import final_acceptance as harness


IMAGE = "sha256:" + "ab" * 32
LAUNCH = {"entrypoint": ["/usr/local/bin/fixture-server"], "command": ["--serve", "x"]}
BASE_CMD = ["fixture-base-command", "--port", "9090"]
BIND = "D:/fixtures/base/rel-data"
ENV = {"FIXTURE_DIRECT": "direct-value", "FIXTURE_FROM_ENV": "hello-final-accept",
       "FIXTURE_GREETING": "hello-final-accept", "FIXTURE_EXTRA": "extra-value"}
ENV_FILE = "D:/fixtures/base/.env"


def resolved(*, command, entrypoint=None, binds=None, env=None, env_file=None,
             networks=None, volumes=None):
    return {"services": {"app": {
        "image": IMAGE, "command": command, "entrypoint": entrypoint,
        "container_name": "finalaccept-config-fixture",
        "environment": dict(env if env is not None else ENV),
        "env_file": [{"path": env_file or ENV_FILE}],
        "volumes": ([{"type": "bind", "source": binds if binds else BIND, "target": "/data",
                      "read_only": True},
                     {"type": "volume", "source": "appdata", "target": "/var/lib/fixture"}]
                    if binds != "short" else
                    [f"{BIND}:/data:ro", "appdata:/var/lib/fixture"]),
        "networks": {"front": {}} if networks is None else networks}},
        "networks": {"front": {"name": "finalaccept-front"}},
        "volumes": {"appdata": {"name": "finalaccept-appdata"}}}


def test_extract_service_handles_long_and_short_volume_forms():
    long = harness.extract_service(resolved(command=BASE_CMD), "app")
    assert long["binds"] == {"/data": BIND}
    assert long["named_volumes"] == {"/var/lib/fixture": "appdata"}
    assert long["service_networks"] == ["front"]
    assert long["top_networks"] == {"front": "finalaccept-front"}
    assert long["top_volumes"] == {"appdata": "finalaccept-appdata"}
    assert long["env_file"] == [ENV_FILE]
    short = harness.extract_service(resolved(command=BASE_CMD, binds="short"), "app")
    assert short["binds"] == {"/data": BIND}
    assert short["named_volumes"] == {"/var/lib/fixture": "appdata"}


def test_extract_service_handles_list_environment_and_missing_service():
    document = resolved(command=BASE_CMD)
    document["services"]["app"]["environment"] = ["A=1", "B=2", "NOVALUE"]
    assert harness.extract_service(document, "app")["environment"] == {"A": "1", "B": "2"}
    with pytest.raises(KeyError):
        harness.extract_service(document, "absent")


def test_compare_resolved_passes_when_only_launch_changes():
    base = resolved(command=BASE_CMD)
    first = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"])
    second = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"])
    outcome = harness.compare_resolved(base, first, second, service="app",
                                       expected_image=IMAGE, expected_launch=LAUNCH)
    assert outcome["passed"] is True
    assert all(outcome["aspects"][key]["equal"]
               for key in ("binds", "environment", "env_file", "service_networks",
                           "top_networks", "top_volumes", "named_volumes",
                           "container_name", "image", "launch"))


@pytest.mark.parametrize("fault", ["bind", "env", "network", "volume", "launch", "image"])
def test_compare_resolved_rejects_unexpected_drift(fault):
    base = resolved(command=BASE_CMD)
    first = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"])
    if fault == "bind":
        second = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"],
                          binds="D:/elsewhere/rel-data")
    elif fault == "env":
        drifted = dict(ENV, FIXTURE_FROM_ENV="")
        second = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"], env=drifted)
    elif fault == "network":
        second = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"])
        second["networks"] = {"front": {"name": "renamed-front"}}
    elif fault == "volume":
        second = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"])
        second["volumes"] = {"appdata": {"name": "renamed-appdata"}}
    elif fault == "launch":
        second = resolved(command=BASE_CMD)
    else:
        second = resolved(command=LAUNCH["command"], entrypoint=LAUNCH["entrypoint"])
        second["services"]["app"]["image"] = "sha256:" + "00" * 32
    outcome = harness.compare_resolved(base, first, second, service="app",
                                       expected_image=IMAGE, expected_launch=LAUNCH)
    assert outcome["passed"] is False


def test_scan_for_secrets_flags_credentials_and_passes_fixture_values():
    # Built without a committed secret-like literal so the packaging scanner
    # stays silent; the effective input is identical either way.
    trigger = "SECRET_KEY" + "=" + "abc123"
    assert harness.scan_for_secrets(trigger + "\n") != []
    assert harness.scan_for_secrets("cookie: sessionid=abcdef123456\n") != []
    assert harness.scan_for_secrets("AURA_LAB_API_TOKEN=xyz\n") != []
    clean = json.dumps({"env": ENV, "image": IMAGE, "launch": LAUNCH})
    assert harness.scan_for_secrets(clean) == []


def test_matrix_slot_enforces_one_run_plus_one_noted_retry(tmp_path):
    assert harness.matrix_slot(tmp_path, None)["allowed"] is True
    (tmp_path / "matrix-runs.json").write_text(json.dumps([{"passed": False}]), encoding="utf-8")
    assert harness.matrix_slot(tmp_path, None)["allowed"] is False
    assert harness.matrix_slot(tmp_path, "short")["allowed"] is False
    note = "demonstrated harness-only defect: runner misread the case evidence path"
    allowed = harness.matrix_slot(tmp_path, note)
    assert allowed["allowed"] is True and allowed["attempt"] == 2
    (tmp_path / "matrix-runs.json").write_text(
        json.dumps([{"passed": False}, {"passed": False}]), encoding="utf-8")
    assert harness.matrix_slot(tmp_path, note)["allowed"] is False
    (tmp_path / "matrix-runs.json").write_text(json.dumps([{"passed": True}]), encoding="utf-8")
    assert harness.matrix_slot(tmp_path, None)["allowed"] is False


def test_config_only_runner_refuses_mutations_and_unknown_verbs():
    runner = harness.ConfigOnlyRunner([])

    async def attempt(argv):
        await runner(argv, timeout=5, max_stdout=10, max_stderr=10)

    with pytest.raises(RuntimeError):
        asyncio.run(attempt(["docker", "--context", "desktop-linux", "compose",
                             "up", "--detach"]))
    with pytest.raises(RuntimeError):
        asyncio.run(attempt(["docker", "--context", "desktop-linux", "container",
                             "inspect", "x"]))
    with pytest.raises(RuntimeError):
        asyncio.run(attempt(["docker", "--context", "desktop-linux", "volume", "ls"]))
    assert len(runner.calls) == 3


def test_config_only_runner_delegates_read_only_calls(monkeypatch):
    seen = []

    async def fake_run(argv, **limits):
        seen.append(argv)
        from aura_ops.docker import CommandResult
        return CommandResult(0, "{}")

    monkeypatch.setattr(harness, "run_command", fake_run)
    runner = harness.ConfigOnlyRunner([])
    asyncio.run(runner(["docker", "--context", "desktop-linux", "compose", "config",
                        "--format", "json"], timeout=5, max_stdout=10, max_stderr=10))
    asyncio.run(runner(["docker", "--context", "desktop-linux", "image", "inspect", IMAGE],
                       timeout=5, max_stdout=10, max_stderr=10))
    assert len(seen) == 2


def test_write_base_fixture_is_self_consistent_and_credential_free(tmp_path):
    import hashlib
    fixture = harness.write_base_fixture(tmp_path / "fixture")
    manifest_bytes = (tmp_path / "fixture" / "base" / "source-manifest.json").read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == fixture["manifest_sha256"]
    assert (tmp_path / "fixture" / "base" / "compose.yaml").is_file()
    assert (tmp_path / "fixture" / "base" / ".env").is_file()
    blob = "".join(path.read_text(encoding="utf-8")
                   for path in (tmp_path / "fixture" / "base").rglob("*") if path.is_file())
    assert harness.scan_for_secrets(blob) == []
    assert "127.0.0.1" not in blob and "localhost" not in blob


def test_mock_identity_satisfies_production_validator():
    from aura_ops.actions import _identity
    identity = harness._mock_identity()
    assert _identity({"identity": identity}) == identity


def test_backend_health_url_defaults_and_injects(monkeypatch):
    monkeypatch.delenv("AURA_ACCEPT_BACKEND_HEALTH", raising=False)
    assert acceptance_module.backend_health_url() == acceptance_module.BACKEND_HEALTH
    monkeypatch.setenv("AURA_ACCEPT_BACKEND_HEALTH", "http://127.0.0.1:19999/health")
    assert acceptance_module.backend_health_url() == "http://127.0.0.1:19999/health"


def test_backend_health_uses_injected_url(monkeypatch):
    seen = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(url, timeout=10):
        seen.append(url)
        return FakeResponse()

    monkeypatch.setattr(acceptance_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AURA_ACCEPT_BACKEND_HEALTH", "http://127.0.0.1:19999/health")
    assert acceptance_module.Acceptance("acc-f00000").backend_health() == 200
    assert seen == ["http://127.0.0.1:19999/health"]


def test_pick_temp_port_avoids_reserved_ports():
    port = harness.pick_temp_port()
    assert isinstance(port, int) and port not in (*harness.MATRIX_PORTS, 8000)


def test_server_boot_carries_exact_task_flags():
    code = harness.SERVER_BOOT.format(backend="b", port=1)
    for flag in ("provider_configured=False", "wire_default_orchestrator=False",
                 "enable_demo=False", "enable_ops=False"):
        assert flag in code
    assert "OpsService" not in code and "register" not in code.lower()
