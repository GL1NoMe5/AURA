"""OPS-RELEASE-002-R1 packager tests (offline, no provider requests).

Secret-shaped literals below are always built from fragments so this
file itself stays clean under the real package scan.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
for _path in (str(_BACKEND),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

_spec = importlib.util.spec_from_file_location(
    "release_package", _REPO / "tools" / "release" / "package.py")
assert _spec is not None and _spec.loader is not None
package = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(package)

TOKEN = "sk-" + "abcdefghij" + "1234567890"
QUOTED_VALUE = "VX9!" + "q2#mZ8$pL"
PUNCT_VALUE = "k7!qx" + "$9vTp#2"


def _skeleton(root: Path) -> None:
    """Minimal fake project tree satisfying the real allowlist."""
    for name in list(package.ALLOW_FILES) + list(package.MAPPING):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"placeholder for {name}\n".encode("utf-8"))
    for tree in package.ALLOW_TREES:
        sample = root / tree / "sample.txt"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text("harmless sample\n", encoding="utf-8")
    for name in package.REQUIRED:
        target = root / name
        if target.is_file():
            continue
        if name in package.MAPPING.values():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"placeholder for {name}\n".encode("utf-8"))


def test_allowlist_has_no_forbidden_paths():
    names = list(package.ALLOW_FILES) + list(package.ALLOW_TREES) \
        + list(package.MAPPING) + list(package.MAPPING.values())
    for name in names:
        assert package.forbidden_reason(name) is None, name
        package._check_lexical(name, "member")
    for name in package.REQUIRED:
        assert name in package.ALLOW_FILES or name in package.MAPPING.values() \
            or any(name.startswith(tree + "/")
                   for tree in package.ALLOW_TREES), name
    assert "README.md" not in package.ALLOW_FILES
    assert package.MAPPING == {"tools/release/PACKAGE_README.md": "README.md"}
    for name in ("tools/ops/lab.py", "tools/ops/acceptance.py",
                 "tools/ops/final_acceptance.py", "tools/ops/check_upstream.py",
                 "tools/ops/seed_linkding.py", "tools/ops/seed_healthchecks.py",
                 "infra/ops/linkding.Dockerfile", "infra/ops/linkding-test.Dockerfile",
                 "backend/aura_ops/test_runner.py", "backend/aura_ops/test_protocol.py",
                 "tools/ops/healthchecks_gateway.py",
                 "tools/ops/lab_gateway.py",
                 "tools/preflight/soak_check.py",
                 "tools/preflight/baseten_probe.py",
                 "tools/preflight/fixtures/vision-probe.png",
                 "resources/ops/test-requirements.txt",
                 "resources/ops/healthchecks.json",
                 "docs/RELEASE_READINESS.md"):
        assert name in package.REQUIRED, name


def test_packaged_tests_include_their_direct_tools_imports():
    members = package.collect_members(_REPO)
    for rel, path in members.items():
        if not rel.startswith("backend/tests/") or not rel.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [entry.name for entry in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module, *(node.module + "." + entry.name for entry in node.names)]
            else:
                continue
            for module in modules:
                if not module.startswith("tools."):
                    continue
                dependency = module.replace(".", "/") + ".py"
                if (_REPO / dependency).is_file():
                    assert dependency in members, (rel, dependency)


def test_private_files_are_excluded(tmp_path):
    _skeleton(tmp_path)
    private = [
        ".local/ops/store.sqlite",
        ".env",
        "backend/.venv/Scripts/python.exe",
        "backend/aura_api/__pycache__/app.pyc",
        "frontend/node_modules/react/index.js",
        "frontend/dist/index.html",
        "assets/portrait/AURAv2.png",
        "resources/models/depth-anything-v2-small/weights.pth",
        "resources/vendor/depth-anything-v2/app.py",
        "tools/ops/checkpoint.py",
        "tools/preflight/ui_fixture.py",
    ]
    for name in private:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"private\n")
    members = package.collect_members(tmp_path)
    for name in private:
        assert name not in members, name
    assert ".env.example" in members
    assert members["README.md"].read_bytes() == (
        tmp_path / "tools/release/PACKAGE_README.md").read_bytes()


def test_dotenv_variants_are_rejected():
    for name in ("frontend/public/.env.production", ".env", ".ENV",
                 "backend/tests/.Env.local", "frontend/tests/.env.example",
                 "a/.env.setup-tmp"):
        assert package.forbidden_reason(name) is not None, name
    assert package.forbidden_reason(".env.example") is None
    assert package.forbidden_reason(".ENV.EXAMPLE") is None


def test_dotenv_tree_file_fails_collection(tmp_path):
    _skeleton(tmp_path)
    target = tmp_path / "frontend" / "public" / ".env.production"
    target.write_text("anything\n", encoding="utf-8")
    with pytest.raises(package.PackagingError, match="dotenv"):
        package.collect_members(tmp_path)


def test_lexical_escapes_are_rejected(tmp_path):
    _skeleton(tmp_path)
    for bad in ("../outside.txt", "/abs.txt", "C:/win.txt", "a\\b.txt",
                "a/./b.txt", "a/../b.txt", "trailing /x.txt"):
        with pytest.raises(package.PackagingError):
            package.collect_members(
                tmp_path, allow_files=[bad], allow_trees=[], mapping={},
                required=[])


def test_likely_secrets_fail_with_paths_only(tmp_path, capsys):
    _skeleton(tmp_path)
    leaked = tmp_path / "backend" / "aura_api" / "leaked.py"
    leaked.write_text(f'KEY = "{TOKEN}"\n', encoding="utf-8")
    output = tmp_path / "out" / "aura-clean.zip"
    with pytest.raises(package.PackagingError):
        package.build_package(tmp_path, output)
    assert not output.is_file()
    report = output.with_suffix(output.suffix + ".checks.json")
    assert report.is_file()
    content = report.read_text(encoding="utf-8")
    assert "backend/aura_api/leaked.py" in content
    assert "provider-key-token" in content
    assert TOKEN not in content
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_quoted_json_assignment_flags(tmp_path):
    _skeleton(tmp_path)
    leaked = tmp_path / "backend" / "aura_api" / "config.json"
    leaked.write_text('{"api_key": "' + QUOTED_VALUE + '"}\n',
                      encoding="utf-8")
    with pytest.raises(package.PackagingError):
        package.build_package(tmp_path, tmp_path / "out.zip")


def test_punctuation_value_flags(tmp_path):
    _skeleton(tmp_path)
    leaked = tmp_path / "backend" / "aura_api" / "settings.py"
    leaked.write_text("password = " + '"' + PUNCT_VALUE + '"\n',
                      encoding="utf-8")
    with pytest.raises(package.PackagingError):
        package.build_package(tmp_path, tmp_path / "out.zip")


def test_placeholder_substring_no_longer_exempts(tmp_path):
    _skeleton(tmp_path)
    leaked = tmp_path / "backend" / "aura_api" / "other.py"
    leaked.write_text(
        "password = " + '"con' + 'test-winner-2026-x7"\n', encoding="utf-8")
    with pytest.raises(package.PackagingError):
        package.build_package(tmp_path, tmp_path / "out.zip")


def test_large_text_is_scanned_not_skipped(tmp_path):
    _skeleton(tmp_path)
    big = tmp_path / "backend" / "aura_api" / "big.txt"
    big.write_text("x" * 300000 + "\npassword = " + '"'
                   + PUNCT_VALUE + '"\n', encoding="utf-8")
    assert big.stat().st_size > 256 * 1024
    with pytest.raises(package.PackagingError):
        package.build_package(tmp_path, tmp_path / "out.zip")


def test_suppression_needs_exact_line_and_path(tmp_path, monkeypatch):
    line = "password = " + '"con' + 'test-winner-2026-x7"'
    digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
    _skeleton(tmp_path)
    rel = "backend/tests/sample_secret.py"
    (tmp_path / rel).write_text(line + "\n", encoding="utf-8")
    with pytest.raises(package.PackagingError):
        package.build_package(tmp_path, tmp_path / "out.zip")
    monkeypatch.setitem(
        package.REVIEWED, rel,
        {digest: {"line": 1, "reason": "test-only narrow exception"}})
    result = package.build_package(tmp_path, tmp_path / "out2.zip")
    checks = json.loads(result["checks_path"].read_text(encoding="utf-8"))
    assert [entry["path"] for entry in checks["suppressed_findings"]] == [rel]
    other = "backend/tests/other_secret.py"
    (tmp_path / other).write_text(line + "\n", encoding="utf-8")
    with pytest.raises(package.PackagingError):
        package.build_package(tmp_path, tmp_path / "out3.zip")


def test_real_false_positive_patterns_do_not_flag(tmp_path):
    _skeleton(tmp_path)
    sample = tmp_path / "backend" / "aura_api" / "code.py"
    sample.write_text(
        "api_key=self._api_key, base_url=BASE_URL,\n"
        "api_key: str | None = None\n"
        "key = os.environ.get(\n"
        "PATTERN = " + '"----' + '-BEGIN (?:[A-Z ]*PRIVATE KEY)----' + '-"\n'
        "before\n-----BEGIN RSA PRIVATE KEY-----\nprivate content\n"
        "-----END RSA PRIVATE KEY-----\nafter\n",
        encoding="utf-8")
    output = tmp_path / "out" / "aura-clean.zip"
    result = package.build_package(tmp_path, output)
    assert output.is_file()
    assert result["manifest"]["member_count"] == len(
        package.collect_members(tmp_path))


def test_unquoted_templates_are_not_literals(tmp_path):
    _skeleton(tmp_path)
    sample = tmp_path / "backend" / "aura_api" / "refs.py"
    sample.write_text(
        "api_key=${VAULT_KEY}\npassword={generated}\n", encoding="utf-8")
    output = tmp_path / "out" / "aura-clean.zip"
    assert package.build_package(tmp_path, output)


def test_quoted_templates_stay_strict(tmp_path):
    _skeleton(tmp_path)
    leaked = tmp_path / "backend" / "aura_api" / "quoted_ref.py"
    leaked.write_text('password = "$' + '{VAULT_KEY_1}"\n', encoding="utf-8")
    with pytest.raises(package.PackagingError):
        package.build_package(tmp_path, tmp_path / "out.zip")


def test_real_tree_has_no_unsuppressed_findings():
    members = package.collect_members(_REPO)
    scan = package.scan_members(members)
    assert scan["findings"] == []
    for entry in scan["suppressed"]:
        assert entry["path"] in members
        assert entry["reason"] and TOKEN not in entry["reason"]
    for path in package.REVIEWED:
        assert path in members, path
    # Pinned inventory: 30 reviewed unique lines suppressing 32 hit lines
    # (two lines repeat). Update deliberately when fixtures change.
    assert sum(len(hashes) for hashes in package.REVIEWED.values()) == 30
    assert len(scan["suppressed"]) == 32
    assert package.check_markdown_links(members) == []


@pytest.mark.parametrize("rel, number", [
    ("backend/tests/test_ops_patch_guards.py", 45),
    ("backend/tests/test_ops_patch_guards.py", 58),
    ("backend/tests/test_ops_reconciliation.py", 682),
    ("tools/ops/seed_healthchecks.py", 13),
])
def test_reviewed_marker_exceptions_require_exact_content_and_path(rel, number):
    """Reviewed fixture markers never grant a file-wide or value-wide bypass."""
    line = (_REPO / rel).read_text(encoding="utf-8").splitlines()[number - 1]
    findings, suppressed = package.scan_text(rel, line)
    assert findings == []
    assert len(suppressed) == 1
    assert suppressed[0]["reviewed_line"] == number
    for path, content in ((rel, line + " # changed"),
                          ("backend/tests/unreviewed_fixture.py", line)):
        findings, suppressed = package.scan_text(path, content)
        assert len(findings) == 1
        assert suppressed == []


def test_tree_root_junction_mocked(tmp_path, monkeypatch):
    _skeleton(tmp_path)
    real = package._is_link_or_reparse

    def fake(path):
        if Path(path).as_posix().endswith("backend/aura_api"):
            return True
        return real(path)

    monkeypatch.setattr(package, "_is_link_or_reparse", fake)
    with pytest.raises(package.PackagingError, match="[Ll]ink|[Rr]eparse"):
        package.collect_members(tmp_path)


def test_file_ancestor_link_mocked(tmp_path, monkeypatch):
    _skeleton(tmp_path)
    real = package._is_link_or_reparse

    def fake(path):
        if Path(path).as_posix().endswith("resources/licenses"):
            return True
        return real(path)

    monkeypatch.setattr(package, "_is_link_or_reparse", fake)
    with pytest.raises(package.PackagingError, match="[Ll]ink|[Rr]eparse"):
        package.collect_members(tmp_path)


def test_entry_link_mocked(tmp_path, monkeypatch):
    _skeleton(tmp_path)
    real = package._is_link_or_reparse

    def fake(path):
        if Path(path).as_posix().endswith("aura_api/sample.txt"):
            return True
        return real(path)

    monkeypatch.setattr(package, "_is_link_or_reparse", fake)
    with pytest.raises(package.PackagingError, match="[Ll]ink|[Rr]eparse"):
        package.collect_members(tmp_path)


def test_symlink_is_rejected_when_creatable(tmp_path):
    _skeleton(tmp_path)
    link = tmp_path / "backend" / "aura_api" / "evil-link.py"
    try:
        link.symlink_to(tmp_path / "README.md")
    except OSError:
        pytest.skip("Symlink creation needs privileges on this machine.")
    with pytest.raises(package.PackagingError, match="[Ll]ink|[Rr]eparse"):
        package.collect_members(tmp_path)


def test_mapping_collision_and_missing_source(tmp_path):
    _skeleton(tmp_path)
    with pytest.raises(package.PackagingError, match="[Cc]ollides"):
        package.collect_members(
            tmp_path, allow_files=["docs/CLEAN_START.md"], allow_trees=[],
            mapping={"tools/release/README.md": "docs/CLEAN_START.md"},
            required=[])
    with pytest.raises(package.PackagingError, match="[Mm]issing"):
        package.collect_members(
            tmp_path, allow_files=[], allow_trees=[],
            mapping={"docs/no-such-file.md": "README.md"}, required=[])


def test_markdown_links_must_resolve(tmp_path):
    (tmp_path / "sub").mkdir(parents=True)
    (tmp_path / "a.md").write_text(
        "[ok](sub/b.md) [ext](https://example.com/x) [a](#t)\n",
        encoding="utf-8")
    (tmp_path / "sub" / "b.md").write_text("leaf\n", encoding="utf-8")
    (tmp_path / "broken.md").write_text("[bad](nope.md)\n", encoding="utf-8")
    (tmp_path / "escape.md").write_text("[up](../outside.md)\n",
                                        encoding="utf-8")
    good = package.collect_members(
        tmp_path, allow_files=["a.md", "sub/b.md"], allow_trees=[],
        mapping={}, required=[])
    assert package.check_markdown_links(good) == []
    bad = package.collect_members(
        tmp_path, allow_files=["a.md", "sub/b.md", "broken.md",
                               "escape.md"],
        allow_trees=[], mapping={}, required=[])
    broken = package.check_markdown_links(bad)
    assert {entry["path"] for entry in broken} == {"broken.md", "escape.md"}


def test_binaries_are_recorded_as_unscanned(tmp_path):
    _skeleton(tmp_path)
    (tmp_path / "frontend" / "public" / "extra.bin").write_bytes(
        b"\xff\xfe\x00binary\x01")
    output = tmp_path / "out" / "aura-clean.zip"
    result = package.build_package(tmp_path, output)
    checks = json.loads(result["checks_path"].read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in checks["unscanned_files"]]
    assert "frontend/public/extra.bin" in paths
    assert checks["scanned_text_files"] + len(paths) == checks["members"]


def test_roundtrip_preserves_source_bytes(tmp_path):
    _skeleton(tmp_path)
    output = tmp_path / "out" / "aura-clean.zip"
    result = package.build_package(tmp_path, output)
    assert result["checks_path"].is_file()
    assert result["manifest_path"].is_file()
    assert len(result["manifest"]["package_sha256"]) == 64
    assert "total_bytes_note" in result["manifest"]
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(output) as archive:
        for info in archive.infolist():
            assert ".." not in Path(info.filename).parts
            assert not Path(info.filename).is_absolute()
        archive.extractall(extracted)
    for entry in result["manifest"]["members"]:
        source = entry.get("source", entry["path"])
        size, sha256 = package.sha256_file(extracted / entry["path"])
        expected_size, expected_hash = package.sha256_file(tmp_path / source)
        assert size == entry["bytes"] == expected_size, entry["path"]
        assert sha256 == entry["sha256"] == expected_hash, entry["path"]
    # The delivered tool must also work from the delivered source tree.
    # Merely collecting pytest imports did not catch an omitted mapped source.
    rebuilt_members = package.collect_members(extracted)
    assert set(rebuilt_members) == {entry["path"] for entry in result["manifest"]["members"]}
