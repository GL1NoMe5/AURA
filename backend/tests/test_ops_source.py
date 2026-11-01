"""Read boundaries and immutable source evidence."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_ops.models import OpsError, TargetSpec
from aura_ops.source import SourceIndex


def source_spec(tmp_path, files=None):
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.py").write_text("answer = 42\n", encoding="utf-8")
    files = files or {"app.py": hashlib.sha256((root / "app.py").read_bytes()).hexdigest()}
    (tmp_path / "source-manifest.json").write_text(json.dumps({"source_dir": str(root), "revision": "a" * 40, "files": files}), encoding="utf-8")
    return TargetSpec(target_id="alpha", name="Alpha", container_name="alpha",
                      probes=[{"url": "http://127.0.0.1:18090/health"}], source_dir=str(root), source_revision="a" * 40)


def test_source_read_carries_revision_hash_and_line_numbers(tmp_path):
    source = SourceIndex(source_spec(tmp_path))
    result = source.read("app.py")
    assert result["revision"] == "a" * 40
    assert result["content"] == "1: answer = 42"
    assert result["sha256"] == source.files["app.py"]
    assert not result["truncated"]


def test_source_changed_after_index_is_rejected(tmp_path):
    spec = source_spec(tmp_path)
    source = SourceIndex(spec)
    (Path(spec.source_dir) / "app.py").write_text("answer = 43\n")
    with pytest.raises(OpsError) as error:
        source.read("app.py")
    assert error.value.code == "source_changed"


def test_rewriting_manifest_cannot_rebind_the_enrolled_revision(tmp_path):
    spec = source_spec(tmp_path)
    source = SourceIndex(spec)
    spec.source_manifest_sha256 = source.provenance_hash
    path = Path(spec.source_dir) / "app.py"
    path.write_text("answer = 999\n")
    manifest_path = tmp_path / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["app.py"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(OpsError) as error:
        SourceIndex(spec)
    assert error.value.code == "source_manifest_changed"


@pytest.mark.parametrize("name", ["../outside.py", "/absolute.py", "C:/outside.py", "dir\\outside.py", ".hidden.py"])
def test_manifest_cannot_authorize_source_path_escape(tmp_path, name):
    spec = source_spec(tmp_path, {name: "a" * 64})
    source = SourceIndex(spec)
    with pytest.raises(OpsError) as error:
        source.read(name)
    assert error.value.code == "source_path_forbidden"


def test_source_does_not_read_env_or_unknown_files(tmp_path):
    spec = source_spec(tmp_path)
    (Path(spec.source_dir) / ".env").write_text("TOKEN=do-not-read")
    source = SourceIndex(spec)
    assert source.inventory()["files"] == ["app.py"]
    with pytest.raises(OpsError):
        source.read(".env")


def test_symlink_to_file_outside_source_is_rejected(tmp_path):
    spec = source_spec(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    link = Path(spec.source_dir) / "app.py"
    link.unlink()
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or elevated privileges")
    source = SourceIndex(spec)
    with pytest.raises(OpsError) as error:
        source.read("app.py")
    assert error.value.code == "source_path_forbidden"
