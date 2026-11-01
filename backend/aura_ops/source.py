"""Bounded source reads with an explicit enrolled revision and file provenance."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

from .models import OpsError, TargetSpec


class SourceIndex:
    def __init__(self, spec: TargetSpec):
        self.root = Path(spec.source_dir).resolve() if spec.source_dir else None
        self.revision = spec.source_revision
        self.files: dict[str, str] = {}
        self.provenance_hash = None
        self.overlay = None
        if self.root is None:
            return
        if not self.root.is_dir():
            raise OpsError("source_missing", "The enrolled source directory is missing.", 409)
        manifest = self.root.parent / "source-manifest.json"
        if manifest.is_file():
            if manifest.stat().st_size > 2_000_000:
                raise OpsError("source_manifest_invalid", "Source manifest is too large.", 409)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if Path(data.get("source_dir", "")).resolve() != self.root or data.get("revision") != self.revision:
                raise OpsError("source_revision_mismatch", "Source manifest does not match enrollment.", 409)
            self.files = {p: h for p, h in data.get("files", {}).items() if p.endswith(".py")}
            self.provenance_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
            self.overlay = data.get("verified_overlay")
            if self.overlay and self.overlay.get("image_id") != spec.source_image_id:
                raise OpsError("source_image_unverified", "Verified source overlay belongs to another image.", 409)
        elif (self.root / ".git").exists():
            def git(*args):
                try:
                    return subprocess.check_output(["git", "-C", str(self.root), *args], timeout=10, stderr=subprocess.DEVNULL).decode("utf-8").strip()
                except (OSError, subprocess.SubprocessError):
                    raise OpsError("source_unverified", "Cannot verify the enrolled source revision.", 409) from None
            if git("rev-parse", "HEAD") != self.revision or git("status", "--porcelain", "--untracked-files=no"):
                raise OpsError("source_revision_mismatch", "Source must be clean and match its enrolled commit.", 409)
            for name in git("ls-files", "--", "*.py").splitlines()[:2000]:
                path = self._path(name)
                if path.is_file() and path.stat().st_size <= 512_000:
                    self.files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.provenance_hash = hashlib.sha256(json.dumps(self.files, sort_keys=True).encode()).hexdigest()
        else:
            raise OpsError("source_unverified", "An exact clean Git revision or source manifest is required.", 409)
        if spec.source_manifest_sha256 and spec.source_manifest_sha256 != self.provenance_hash:
            raise OpsError("source_manifest_changed", "Source provenance changed since enrollment.", 409)

    def _path(self, name: str) -> Path:
        pure = PurePosixPath(name)
        if not self.root or pure.is_absolute() or ".." in pure.parts or "\\" in name or ":" in name:
            raise OpsError("source_path_forbidden", "Source path is outside the enrolled tree.", 403)
        path = self.root.joinpath(*pure.parts).resolve()
        if not path.is_relative_to(self.root) or any(part.startswith(".") for part in pure.parts):
            raise OpsError("source_path_forbidden", "Source path is outside the enrolled tree.", 403)
        return path

    def inventory(self) -> dict:
        return {"revision": self.revision, "files": sorted(self.files)[:400], "total_files": len(self.files),
                "verified_overlay": self.overlay}

    def read(self, name: str) -> dict:
        if name not in self.files:
            raise OpsError("source_path_forbidden", "Only enrolled Python source files may be read.", 403)
        path = self._path(name)
        if not path.is_file() or path.stat().st_size > 512_000:
            raise OpsError("source_unavailable", "Source file is missing or exceeds the read limit.", 409)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.files[name]:
            raise OpsError("source_changed", "Source changed since its verified revision.", 409)
        text = content.decode("utf-8", errors="replace")
        lines = text.splitlines()
        numbered = "\n".join(f"{n}: {line}" for n, line in enumerate(lines, 1))
        return {"path": name, "revision": self.revision, "verified_overlay": self.overlay, "sha256": self.files[name],
                "content": numbered[:24000], "truncated": len(numbered) > 24000, "line_count": len(lines)}
