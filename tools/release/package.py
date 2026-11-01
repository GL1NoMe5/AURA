"""Explicit-allowlist source packager for a clean local AURA copy (R1).

Builds a reviewable ZIP that contains only what a fresh folder needs to
install, build, start and collection-check AURA, plus required
third-party notices. Nothing is published. This is release engineering,
not a security audit.

Included: launch/config files, backend and runtime sources, test helpers
and static fixtures, locked dependency metadata, frontend source with
derived portrait/audio assets, and source licenses/notices. The package
root README is a labelled package-specific document mapped from
tools/release/PACKAGE_README.md; the repository README stays in the repo.

Never included: .local, .git, .agents, virtual environments, node_modules,
build output, raw portrait sources, model weights, credentials (including
.env variants), runtime databases, or private generated workspaces.
Symlinks/reparse points and paths escaping the project root are rejected
with an error, including links on ancestor directories.

Default output: .local/release/OPS-RELEASE-002/aura-clean-<UTC>.zip
Sidecars: .zip.manifest.json (members, sizes, SHA-256, archive hash) and
.zip.checks.json (forbidden-path, likely-secret, and Markdown-link
report; paths/rule names only, never matched values).

Usage:
    python tools/release/package.py [--project-root ROOT] [--output ZIP]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

TOOL = "tools/release/package.py (OPS-RELEASE-002-R1)"
TASK_DIR = Path(".local/release/OPS-RELEASE-002")
ZIP_DATE_TIME = (2026, 1, 1, 0, 0, 0)

# Exact relative files (POSIX separators) copied into the package.
ALLOW_FILES = (
    "LICENSE",
    "MEDIA_LICENSE.md",
    "THIRD_PARTY_NOTICES.md",
    ".env.example",
    "Start-AURA.cmd",
    "Connect-Baseten.cmd",
    "docs/CLEAN_START.md",
    "docs/RELEASE_READINESS.md",
    "docs/FINAL_ACCEPTANCE.md",
    "docs/LIVE_RECOVERY.md",
    "docs/REVIEW_DISPOSITION.md",
    "docs/TEST_EVIDENCE_PROTOCOL.md",
    "docs/PATCH_RETRY_CONTRACT.md",
    "docs/PATCH_EDIT_CONTRACT.md",
    "docs/VERIFICATION_EVIDENCE.md",
    "docs/RECONCILIATION.md",
    "docs/LIFECYCLE_RECOVERY.md",
    "docs/TARGET_MANAGEMENT.md",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "tools/runtime/aura_runtime.py",
    "tools/preflight/configure_baseten.py",
    "tools/preflight/soak_check.py",
    "tools/preflight/live_run.py",
    "tools/preflight/worker_check.py",
    "tools/preflight/baseten_probe.py",
    "tools/preflight/fixtures/vision-probe.png",
    "tools/preflight/fixtures/README.md",
    "tools/ops/lab.py",
    "tools/ops/healthchecks.py",
    "tools/ops/acceptance.py",
    "tools/ops/final_acceptance.py",
    "tools/ops/check_upstream.py",
    "tools/ops/ACCEPTANCE.md",
    "tools/ops/FINAL_ACCEPTANCE.md",
    "tools/ops/check_candidate.py",
    "tools/ops/check_healthchecks.py",
    "tools/ops/live_diagnosis.py",
    "tools/ops/compare_diagnoses.py",
    "tools/ops/soak.py",
    "tools/ops/healthchecks_gateway.py",
    "tools/ops/lab_gateway.py",
    "tools/ops/seed_linkding.py",
    "tools/ops/seed_healthchecks.py",
    "infra/ops/linkding.Dockerfile",
    "infra/ops/linkding-test.Dockerfile",
    "tools/release/package.py",
    "tools/release/PACKAGE_README.md",
    "tools/release/README.md",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
    "resources/README.md",
    "resources/sources.json",
    "resources/licenses/anthropics--skills/LICENSE",
    "resources/licenses/cesartevisual--threejs-skills/LICENSE",
    "resources/licenses/CloudAI-X--threejs-skills/LICENSE",
    "resources/licenses/manrope/LICENSE",
    "resources/licenses/depth-anything-v2/LICENSE",
    "resources/ops/LICENSE.healthchecks.txt",
    "resources/ops/LICENSE.linkding.txt",
    "resources/ops/sources.json",
    "resources/ops/test-requirements.txt",
    "resources/ops/healthchecks.json",
    "resources/references/threejs/LICENSE",
    "resources/vendor/psrdnoise/README.md",
)

# Repo source -> package member renames. The package root README is the
# labelled package document, not the repository README.
MAPPING = {
    "tools/release/PACKAGE_README.md": "README.md",
}

# Directory trees copied recursively with PRUNE applied.
ALLOW_TREES = (
    "backend/aura_api",
    "backend/aura_agents",
    "backend/aura_ops",
    "backend/aura_worker",
    "backend/tests",
    "frontend/src",
    "frontend/public",
    "frontend/tests",
)

# Members that must exist; anything missing fails the build.
REQUIRED = (
    "README.md",
    "tools/release/PACKAGE_README.md",
    "Start-AURA.cmd",
    "Connect-Baseten.cmd",
    ".env.example",
    "docs/CLEAN_START.md",
    "docs/RELEASE_READINESS.md",
    "docs/FINAL_ACCEPTANCE.md",
    "docs/LIVE_RECOVERY.md",
    "docs/REVIEW_DISPOSITION.md",
    "docs/TEST_EVIDENCE_PROTOCOL.md",
    "docs/VERIFICATION_EVIDENCE.md",
    "docs/RECONCILIATION.md",
    "docs/LIFECYCLE_RECOVERY.md",
    "tools/runtime/aura_runtime.py",
    "tools/preflight/configure_baseten.py",
    "tools/preflight/soak_check.py",
    "tools/preflight/live_run.py",
    "tools/preflight/worker_check.py",
    "tools/preflight/baseten_probe.py",
    "tools/preflight/fixtures/vision-probe.png",
    "tools/ops/lab.py",
    "tools/ops/healthchecks.py",
    "tools/ops/acceptance.py",
    "tools/ops/final_acceptance.py",
    "tools/ops/check_upstream.py",
    "tools/ops/ACCEPTANCE.md",
    "tools/ops/FINAL_ACCEPTANCE.md",
    "tools/ops/check_candidate.py",
    "tools/ops/check_healthchecks.py",
    "tools/ops/live_diagnosis.py",
    "tools/ops/compare_diagnoses.py",
    "tools/ops/soak.py",
    "tools/ops/healthchecks_gateway.py",
    "tools/ops/lab_gateway.py",
    "tools/ops/seed_linkding.py",
    "tools/ops/seed_healthchecks.py",
    "infra/ops/linkding.Dockerfile",
    "infra/ops/linkding-test.Dockerfile",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/aura_api/app.py",
    "backend/aura_ops/test_runner.py",
    "backend/aura_ops/test_protocol.py",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/index.html",
    "frontend/vite.config.ts",
    "frontend/public/portrait/particles.bin",
    "frontend/public/portrait/colors.bin",
    "frontend/public/portrait/manifest.json",
    "frontend/public/audio/aura-space.wav",
    "frontend/public/audio/aura-crystal.wav",
    "resources/ops/test-requirements.txt",
    "resources/ops/healthchecks.json",
)

# Names pruned inside allowlisted trees (never packaged). Credential-named
# files are deliberately NOT pruned: they must fail loudly via
# forbidden_reason instead of vanishing silently.
PRUNE_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules",
    "dist", ".local", ".git", ".agents",
})
PRUNE_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")
PRUNE_FILES = frozenset()

# Defense-in-depth: a collected member matching any of these fails the build.
FORBIDDEN_PARTS = frozenset({
    ".local", ".git", ".agents", "__pycache__", ".venv", "venv",
    "node_modules", "dist",
})
FORBIDDEN_SUFFIXES = (
    ".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm",
    ".wal", ".shm", ".pth", ".partial", ".pem", ".key", ".pfx", ".p12",
    ".tsbuildinfo",
)
FORBIDDEN_NAMES = frozenset({"credentials.env"})
FORBIDDEN_PREFIXES = (
    "assets/",
    "resources/models/",
    "resources/vendor/depth-anything-v2/",
    "resources/references/threejs/examples/",
)

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_KEY = r"(password|passwd|api[_-]?key|secret[_-]?key|client[_-]?secret)"
# Quoted literal: JSON '{"api_key": "value"}', TOML/INI key = "value",
# Python password = 'value'. The key name itself may be quoted.
_QUOTED = re.compile(
    _KEY + r"""["']?\s*[:=]\s*["']([^"']{8,})["']""", re.IGNORECASE)
# Unquoted value after '=' only (dotenv/CLI style). A bare code
# expression (identifier, dotted attribute, None/number) is not a finding.
_UNQUOTED = re.compile(
    _KEY + r"""\s*=\s*([^\s"'#;,)]{8,})""", re.IGNORECASE)
_CODE_VALUE = re.compile(
    r"[A-Za-z_]\w*(?:\.\w+)*\(?\)?$|None|True|False|\d+(?:\.\d+)?")
_PEM_BEGIN = "-----BEGIN "
_PEM_END_MARK = "PRIVATE KEY-----"
_B64_RUN = re.compile(r"[A-Za-z0-9+/=]{32,}")
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})")
_CONNSTRING = re.compile(
    r"(?:mongodb|postgres(?:ql)?|mysql|redis)://[^/\s:]+:[^@\s]{8,}@",
    re.IGNORECASE)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# Reviewed exact-line exceptions: relative path -> sha256(stripped line)
# -> {"line": documented line number, "reason": value-free reason}.
# Suppression is content-addressed (no line-number dependence). Every entry
# was classified during agent review on 2026-10-29/30; reports never contain
# matched values. Test-harness placeholder keys are never used for real
# provider calls (mocked transports / injected fake clients).
REVIEWED: dict[str, dict[str, dict]] = {
    "backend/tests/test_agents.py": {
        "5142b7c65066f311f6ef42b4df7762d5c95ebe328a6060ef0c19ead0af661556": {
            "line": 138,
            "reason": "unit-test placeholder key with mocked HTTP transport"},
        "9e3e9d0d33ff5605e164b4d940a6bada20c5a8c5fcb92d21197fd862171d0182": {
            "line": 141,
            "reason": "unit-test placeholder key with mocked HTTP transport"},
        "b8a3255c758c08e14b759729e3137fa6692ada08c8516445f8a714b0a22e1bc9": {
            "line": 328,
            "reason": "unit-test placeholder key with fake client object"},
        "9e7cbff7901930767d3f2d7300920f264f92624f11039f395a070c2f859ee690": {
            "line": 474,
            "reason": "unit-test placeholder key with mocked HTTP transport"},
        "15e1f70f230ed38e17d40d8c21d6b6ca1a2a929d4182209bca548503f446f523": {
            "line": 500,
            "reason": "unit-test placeholder key with fake client class"},
        "83ac1caa967ddce48ba0184da717984a0e63331015d4b7a458282923c96d08c9": {
            "line": 598,
            "reason": "unit-test placeholder key with mocked HTTP transport"},
        "94ab8f714b32147297acb78ca881097e5fc15cec0cfa59a8dac04ea379418336": {
            "line": 601,
            "reason": "unit-test placeholder key with mocked HTTP transport"},
    },
    "backend/tests/test_ops_acceptance.py": {
        "38b4eb3e11b752c04669da5a71fa294747971a4aabe2fb5ffdb907eeb7a9d23a": {
            "line": 49,
            "reason": "redaction-harness mock container Env entry"},
        "7930e47e0b9723bd5535c9a1221acf864a979bd41d70d4633dcede9c0cbf7292": {
            "line": 81,
            "reason": "redaction-harness mock container log line"},
    },
    "backend/tests/test_ops_actions.py": {
        "14ad1d24a2709ee21b5db4aee17507747a44071ea3c874a02401125b49e3c444": {
            "line": 49,
            "reason": "redaction-harness mock inspect fixture"},
        "58f3da790fc49f2ad82fca4d54b567635c97ef3504777ec41bc8153d766d1581": {
            "line": 66,
            "reason": "redaction-harness mock config fixture"},
        "7a85800eaffcfc60c088447800cf943d2cbc6c7b4dbdaf211982f8d36bb6181f": {
            "line": 77,
            "reason": "redaction-harness mock label fixture"},
        "a5893eb60a15cef19bf223eca5baad965937a530b4111b7e6a556b3132e9c4b2": {
            "line": 139,
            "reason": "redaction-harness temp dotenv fixture"},
    },
    "backend/tests/test_ops_deployment.py": {
        "9a91101bc03c2c27514dec7771ffdb39d74288162f07e414cdbd69621209a332": {
            "line": 44,
            "reason": "mock target environment fixture"},
    },
    "backend/tests/test_ops_docker.py": {
        "9ab7bec3b1366dd5243c6f6ed8f461857ac304a2ad31ef0dfbd20e9d31e32285": {
            "line": 96,
            "reason": "redaction unit-test input row"},
        "40de02497ce3d509178bd07f9b58404b3063d99941eb6ba5c580192c235a2dae": {
            "line": 37,
            "reason": "redaction-harness mock Env entry"},
        "82093f21e06f66e1b15249b81869b5a66394bdd30a03d307326c13635349e882": {
            "line": 55,
            "reason": "redaction-harness mock log line"},
    },
    "backend/tests/test_ops_healthchecks.py": {
        "d04d2c2d3c8d923bc93de65dcc603c998516c4ef96b376be75d58eb9e2e77dfc": {
            "line": 65,
            "reason": "credential-leak-guard test with inert marker words"},
        "3d0a711ccc1fe15db788041319db858c7d65d2d5f4bbffaa5672f871049be2bf": {
            "line": 143,
            "reason": "credential-leak-guard test with inert marker words"},
    },
    "backend/tests/test_ops_patches.py": {
        "0200a4fcb5d41b1ad66931f30dd08a8e35dc5650edc3f5006428466726b337f0": {
            "line": 45,
            "reason": "mount-exclusion test source fixture"},
        "04c3235c70127125517a79fd6584accd80b4643bcf0188ee45e31d94970eada6": {
            "line": 39,
            "reason": "mount-exclusion test dotenv fixture"},
    },
    "backend/tests/test_ops_patch_guards.py": {
        "4c169352bf04eb4465b719b36dd84b39f6fe5055d2c4105b37cb022ff566cf3f": {
            "line": 45,
            "reason": "offline mount-exclusion test dotenv fixture with an inert marker"},
        "e29665402d6135c1eea1171d1eaa994ad401d8d32249c529d70bb38d2e0d3bb5": {
            "line": 58,
            "reason": "offline untracked-file exclusion fixture with an inert marker"},
    },
    "backend/tests/test_ops_reconciliation.py": {
        "dff74a1e05fa23b197d3e003ce49cb554383aeef6c555e29e61a6c41be9f5ed8": {
            "line": 682,
            "reason": "operator-note redaction test input checked against durable audit and events"},
    },
    "backend/tests/test_provider_probe.py": {
        "7d0f5d8d9e487d0329877b4c1ba979f7b78a575a44bdb720791565ce69586b85": {
            "line": 188,
            "reason": "unit-test placeholder key with injected fake client"},
    },
    "tools/ops/acceptance.py": {
        "54a4a40615af93ffe670d8f7ce45e09ed7025d2faf509a010ad182e8e08d835c": {
            "line": 923,
            "reason": "redaction display constant for fixture-trace output"},
    },
    "tools/ops/healthchecks.py": {
        "a0e694974cd00b3153347dd131a5bacd6a5be0ff3db90e8fef1c3e5550aac013": {
            "line": 75,
            "reason": "runtime-generated value concatenated in code"},
        "74d372982a0e477916ef068df4cf266eebe55570a57a884d0c32db31c6861ebf": {
            "line": 76,
            "reason": "format-validator regex for the local env file"},
    },
    "tools/ops/seed_healthchecks.py": {
        "9d6f9da8af92e3613d90dcb582bccf7c98f4e3e843c6139f61e0f842f1204196": {
            "line": 13,
            "reason": "stdlib runtime generation of a local fixture credential, not a committed literal"},
    },
    "tools/preflight/soak_check.py": {
        "1c04edcdb05e1fad390801c988bbf6cd4e8843080a7fee94e04e203f5fe9ba12": {
            "line": 31,
            "reason": "mock-transport placeholder constant, never a call"},
    },
}


class PackagingError(RuntimeError):
    pass


def _posix(path: Path) -> str:
    return path.as_posix()


def _is_link_or_reparse(path: Path) -> bool:
    if os.path.islink(path):
        return True
    if os.name == "nt":
        try:
            attrs = os.stat(path, follow_symlinks=False).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attrs & _REPARSE)
    return False


def _within_root(root_real: str, path: Path) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return real == root_real or real.startswith(root_real + os.sep)


def _check_lexical(name: str, kind: str) -> None:
    """Reject absolute, escaping, or noncanonical allowlist names."""
    if not name or name != name.strip() or "\\" in name or "//" in name:
        raise PackagingError(f"Noncanonical {kind} name: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise PackagingError(f"Absolute {kind} path is forbidden: {name}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise PackagingError(
            f"Escaping or empty {kind} segment is forbidden: {name}")


def _validate_chain(root: Path, rel: str) -> Path:
    """Validate every ancestor including the target itself for links."""
    _check_lexical(rel, "member")
    target = root
    for part in rel.split("/"):
        target = target / part
        if _is_link_or_reparse(target):
            raise PackagingError(
                "Refusing symlink/reparse point on path "
                f"{target.relative_to(root).as_posix()} (for {rel})")
    return target


def _collect_tree(root: Path, base: Path,
                  members: dict[str, Path]) -> None:
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError as exc:
            raise PackagingError(
                f"Cannot read directory {current}: {exc}") from None
        for entry in entries:
            entry_path = Path(entry.path)
            if _is_link_or_reparse(entry_path):
                raise PackagingError(
                    "Refusing symlink/reparse point: "
                    f"{entry_path.relative_to(root).as_posix()}")
            if entry.is_dir(follow_symlinks=False):
                if entry.name in PRUNE_DIRS:
                    continue
                stack.append(entry_path)
            elif entry.is_file(follow_symlinks=False):
                if entry.name in PRUNE_FILES:
                    continue
                if entry.name.endswith(PRUNE_SUFFIXES):
                    continue
                rel = _posix(entry_path.relative_to(root))
                members[rel] = entry_path


def collect_members(root: Path, *, allow_files=ALLOW_FILES,
                    allow_trees=ALLOW_TREES, mapping=MAPPING,
                    required=REQUIRED) -> dict[str, Path]:
    """Collect allowlisted members; reject links, escapes, forbidden paths."""
    root = root.resolve()
    root_real = os.path.realpath(root)
    members: dict[str, Path] = {}
    for name in allow_files:
        candidate = _validate_chain(root, name)
        if not candidate.is_file():
            raise PackagingError(f"Allowlisted file is missing: {name}")
        if not _within_root(root_real, candidate):
            raise PackagingError(f"Path escapes the project root: {name}")
        members[name] = candidate
    for src, dst in mapping.items():
        _check_lexical(dst, "mapped member")
        if dst in members:
            raise PackagingError(f"Mapped member collides: {dst}")
        candidate = _validate_chain(root, src)
        if not candidate.is_file():
            raise PackagingError(f"Mapped source is missing: {src}")
        if not _within_root(root_real, candidate):
            raise PackagingError(f"Path escapes the project root: {src}")
        members[dst] = candidate
    for tree in allow_trees:
        base = _validate_chain(root, tree)
        if not base.is_dir():
            raise PackagingError(f"Allowlisted tree is missing: {tree}")
        _collect_tree(root, base, members)
    missing = [name for name in required if name not in members]
    if missing:
        raise PackagingError(
            "Required package members are missing: " + ", ".join(missing))
    for rel in members:
        _check_lexical(rel, "member")
        reason = forbidden_reason(rel)
        if reason is not None:
            raise PackagingError(
                f"Forbidden package member {rel}: {reason}")
        if not _within_root(root_real, members[rel]):
            raise PackagingError(
                f"Path escapes the project root: {rel}")
    return members


def forbidden_reason(rel: str) -> str | None:
    parts = rel.split("/")
    if any(part in FORBIDDEN_PARTS for part in parts):
        return "forbidden path class"
    lowered = rel.lower()
    if any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return "forbidden file type"
    base_low = parts[-1].lower()
    if base_low == ".env" or base_low.startswith(".env."):
        # Only the intended root template is allowed, in any letter case.
        if lowered != ".env.example":
            return "forbidden dotenv credential variant"
    if base_low in FORBIDDEN_NAMES:
        return "forbidden credential file"
    if any(rel.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        return "excluded private/weight/reference source"
    return None


def _pem_hits(lines: list[str]) -> list[int]:
    hits = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(_PEM_BEGIN):
            continue
        if _PEM_END_MARK not in stripped:
            continue
        label = stripped[len(_PEM_BEGIN):]
        if not re.fullmatch(
                r"(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", label):
            continue
        following = " ".join(
            lines[index + 1:index + 4]).replace(" ", "")
        if _B64_RUN.search(following):
            hits.append(index + 1)
    return hits


def _reviewed(rel: str, line: str) -> dict | None:
    digest = hashlib.sha256(line.strip().encode("utf-8")).hexdigest()
    return REVIEWED.get(rel, {}).get(digest)


def scan_text(rel: str, text: str) -> tuple[list[dict], list[dict]]:
    """Return (findings, suppressed) as path/rule/line only, never values."""
    lines = text.splitlines()
    raw: dict[str, list[int]] = {}
    pem_lines = _pem_hits(lines)
    if pem_lines:
        raw["pem-private-key"] = pem_lines
    token_lines: list[int] = []
    quoted_lines: list[int] = []
    unquoted_lines: list[int] = []
    conn_lines: list[int] = []
    for number, line in enumerate(lines, start=1):
        for _match in _TOKEN.finditer(line):
            token_lines.append(number)
        for _match in _QUOTED.finditer(line):
            quoted_lines.append(number)
        for match in _UNQUOTED.finditer(line):
            value = match.group(2)
            if value.startswith(("{", "$")):
                continue  # template/variable reference, not a literal
            if _CODE_VALUE.fullmatch(value):
                continue  # unquoted bare code expression
            unquoted_lines.append(number)
        if _CONNSTRING.search(line):
            conn_lines.append(number)
    if token_lines:
        raw["provider-key-token"] = sorted(set(token_lines))
    if quoted_lines:
        raw["assigned-secret-quoted"] = sorted(set(quoted_lines))
    if unquoted_lines:
        raw["assigned-secret-unquoted"] = sorted(set(unquoted_lines))
    if conn_lines:
        raw["connection-string-secret"] = sorted(set(conn_lines))
    findings: list[dict] = []
    suppressed: list[dict] = []
    for rule, numbers in raw.items():
        kept = []
        for number in numbers:
            entry = _reviewed(rel, lines[number - 1])
            if entry is None:
                kept.append(number)
            else:
                suppressed.append(
                    {"path": rel, "rule": rule, "line": number,
                     "reviewed_line": entry["line"],
                     "reason": entry["reason"]})
        if kept:
            findings.append({"path": rel, "rule": rule, "lines": kept})
    return findings, suppressed


def scan_members(members: dict[str, Path]) -> dict:
    """Scan every decodable text member; classify binaries explicitly."""
    findings: list[dict] = []
    suppressed: list[dict] = []
    unscanned: list[dict] = []
    scanned = 0
    for rel in sorted(members):
        path = members[rel]
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            unscanned.append(
                {"path": rel, "bytes": path.stat().st_size,
                 "reason": "binary/non-UTF-8; classified by inspection, "
                           "not secret-scanned as text"})
            continue
        except OSError:
            unscanned.append(
                {"path": rel, "bytes": -1, "reason": "unreadable file"})
            continue
        scanned += 1
        file_findings, file_suppressed = scan_text(rel, text)
        findings.extend(file_findings)
        suppressed.extend(file_suppressed)
    return {"findings": findings, "suppressed": suppressed,
            "scanned_text_files": scanned, "unscanned_files": unscanned}


def check_markdown_links(members: dict[str, Path]) -> list[dict]:
    """Require every local Markdown link to resolve to a member file."""
    broken: list[dict] = []
    for rel in sorted(members):
        if not rel.lower().endswith(".md"):
            continue
        try:
            text = members[rel].read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            broken.append({"path": rel, "link": "",
                           "detail": "unreadable markdown"})
            continue
        base = PurePosixPath(rel).parent
        for match in _LINK.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith("#"):
                continue
            if _SCHEME.match(target):
                continue
            target = target.split("#", 1)[0].strip()
            if not target:
                continue
            resolved = (base / target).as_posix() \
                if str(base) != "." else target
            resolved = re.sub(r"/{2,}", "/", resolved)
            if resolved == ".." or resolved.startswith("../") \
                    or "/../" in resolved or resolved not in members:
                broken.append({"path": rel, "link": target,
                               "resolved": resolved})
    return broken


def sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def build_package(root: Path, output: Path) -> dict:
    """Collect, scan, link-check, then write ZIP plus manifest/checks."""
    root = root.resolve()
    members = collect_members(root)
    scan = scan_members(members)
    link_findings = check_markdown_links(members)
    checks = {
        "tool": TOOL,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "members": len(members),
        "scanned_text_files": scan["scanned_text_files"],
        "unscanned_files": scan["unscanned_files"],
        "suppressed_findings": scan["suppressed"],
        "forbidden_findings": [],
        "secret_findings": scan["findings"],
        "link_findings": link_findings,
        "note": "Packaging hygiene check only, not a security audit. "
                "Paths/rule names alone; values are never recorded.",
    }
    if scan["findings"] or link_findings:
        output.parent.mkdir(parents=True, exist_ok=True)
        failed_checks = output.with_suffix(
            output.suffix + ".checks.json")
        failed_checks.write_text(
            json.dumps(checks, indent=2) + "\n", encoding="utf-8")
        raise PackagingError(
            "Package checks failed: "
            f"{len(scan['findings'])} likely-secret file(s), "
            f"{len(link_findings)} broken local link(s). "
            f"Finding paths/rule names are in {failed_checks}; "
            "remove the cause and rebuild. Values are never recorded.")
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    total_bytes = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) \
            as archive:
        for rel in sorted(members):
            size, sha256 = sha256_file(members[rel])
            info = zipfile.ZipInfo(rel, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(members[rel], "rb") as handle:
                archive.writestr(info, handle.read())
            source = members[rel].relative_to(root.resolve()).as_posix()
            entry: dict = {"path": rel, "bytes": size, "sha256": sha256}
            if source != rel:
                entry["source"] = source
            entries.append(entry)
            total_bytes += size
    package_bytes, package_sha256 = sha256_file(output)
    manifest = {
        "tool": TOOL,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "package": output.name,
        "package_bytes": package_bytes,
        "package_sha256": package_sha256,
        "member_count": len(entries),
        "total_bytes": total_bytes,
        "total_bytes_note": "Uncompressed sum of member bytes; "
                            "package_bytes is the archive size on disk.",
        "members": entries,
    }
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    checks_path = output.with_suffix(output.suffix + ".checks.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checks_path.write_text(
        json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    return {"manifest": manifest, "manifest_path": manifest_path,
            "checks_path": checks_path}


def default_output(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return root / TASK_DIR / f"aura-clean-{stamp}.zip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.project_root.resolve() if args.project_root \
        else Path(__file__).resolve().parents[2]
    output = args.output if args.output else default_output(root)
    try:
        result = build_package(root, output)
    except PackagingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    manifest = result["manifest"]
    print(f"Package: {output}")
    print(f"SHA-256: {manifest['package_sha256']}")
    print(f"Archive: {manifest['package_bytes']} bytes; "
          f"members: {manifest['member_count']} files, "
          f"uncompressed {manifest['total_bytes']} bytes")
    print(f"Manifest: {result['manifest_path']}")
    print(f"Checks: {result['checks_path']} "
          f"(0 forbidden, 0 likely-secret, 0 broken links)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
