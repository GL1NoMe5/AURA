"""Versioned operations contracts. Model output cannot grant capabilities."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionKind(str, Enum):
    RESTART = "restart_container"
    RESTORE = "restore_config_snapshot"
    ROLLBACK = "rollback_image"
    DEPLOY = "deploy_verified_patch"
    ABSTAIN = "abstain"


class Policy(StrictModel):
    automatic_actions: list[ActionKind] = Field(default_factory=list, max_length=3)
    auto_investigate: bool = False
    restart_cooldown_seconds: int = Field(default=600, ge=600, le=86400)

    @field_validator("automatic_actions")
    @classmethod
    def restricted_automation(cls, value):
        if ActionKind.DEPLOY in value or ActionKind.ABSTAIN in value:
            raise ValueError("deploy requires approval; abstention is not an operation")
        return list(dict.fromkeys(value))


class ProbeSpec(StrictModel):
    url: str = Field(max_length=2048)
    expected_status: int = Field(default=200, ge=200, le=399)
    credential_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    auth_scheme: Literal["Bearer", "Token", "Cookie"] = "Token"
    timeout_seconds: float = Field(default=5, ge=0.1, le=15)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("An HTTP(S) probe URL is required")
        if parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("Credentials, queries and fragments are not allowed in probe URLs")
        return value


class DeploymentLaunch(StrictModel):
    """Operator-enrolled direct server launch, bypassing bootstrap migrations.

    The operator must select an executable whose startup does not migrate data;
    this profile is never constructed from a model's response.
    """
    entrypoint: list[str] = Field(min_length=1, max_length=8)
    command: list[str] = Field(max_length=32)

    @model_validator(mode="after")
    def direct_bounded_launch(self):
        from pathlib import PurePosixPath
        argv = [*self.entrypoint, *self.command]
        if sum(len(arg) for arg in argv) > 4096 or any(
                not arg or len(arg) > 512 or "$" in arg or any(ord(c) < 32 or ord(c) == 127 for c in arg)
                for arg in argv):
            raise ValueError("Deployment launch arguments must be bounded literal values")
        executable = PurePosixPath(self.entrypoint[0])
        if not executable.is_absolute() or ".." in executable.parts or "\\" in self.entrypoint[0] \
                or executable.as_posix() != self.entrypoint[0]:
            raise ValueError("Deployment entrypoint must be an exact absolute POSIX executable")
        if executable.name.lower() in {"sh", "bash", "dash", "zsh", "ksh", "fish", "env", "busybox",
                                       "node", "perl", "ruby"} \
                or executable.name.lower().startswith(("python", "pypy")) \
                or executable.suffix.lower() in {".sh", ".py", ".pl", ".rb", ".js"}:
            raise ValueError("Enroll a direct server executable, not a shell or interpreter launcher")
        if any(arg.lower() in {"migrate", "migrate_tasks", "makemigrations", "upgrade", "manage.py"}
               for arg in argv):
            raise ValueError("Deployment launch must not run database migration commands")
        return self


class TargetSpec(StrictModel):
    target_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    docker_context: str = Field(default="default", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    container_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    probes: list[ProbeSpec] = Field(min_length=1, max_length=3)
    source_dir: str | None = Field(default=None, max_length=1024)
    source_revision: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{40}$")
    source_manifest_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_image_id: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    deployment_root: str | None = Field(default=None, pattern=r"^/[a-zA-Z0-9_./-]+$", max_length=512)
    deployment_launch: DeploymentLaunch | None = None
    compose_file: str | None = Field(default=None, max_length=1024)
    compose_project: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    compose_service: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
    monitoring: bool = True
    policy: Policy = Field(default_factory=Policy)
    patch_profile: "PatchProfile | None" = None
    recovery_checks: list["DownloadCheck"] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def paired_sources(self):
        if bool(self.source_dir) != bool(self.source_revision):
            raise ValueError("source_dir and exact source_revision must be supplied together")
        compose = [self.compose_file, self.compose_project, self.compose_service]
        if any(compose) and not all(compose):
            raise ValueError("compose_file, compose_project and compose_service are required together")
        return self


class InvestigationRequest(StrictModel):
    target_id: str
    question: str = Field(default="Investigate the application's current health.", max_length=1000)


class Finding(StrictModel):
    hypothesis: str = Field(min_length=1, max_length=1500)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)
    confidence: Literal["supported", "possible", "insufficient"]


class Diagnosis(StrictModel):
    summary: str = Field(min_length=1, max_length=2000)
    findings: list[Finding] = Field(default_factory=list, max_length=12)
    action: ActionKind = ActionKind.ABSTAIN
    reason: str = Field(min_length=1, max_length=1500)
    needs_source_analysis: bool = False

    @model_validator(mode="after")
    def evidence_for_action(self):
        if self.action != ActionKind.ABSTAIN and not any(f.confidence == "supported" for f in self.findings):
            raise ValueError("An action requires a supported finding citing supplied evidence")
        return self


def _reject_bool_revision(value):
    if isinstance(value, bool):
        raise ValueError("expected_revision must be a nonnegative integer")
    return value


class UpdateTargetRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    spec: "TargetSpec"

    @field_validator("expected_revision", mode="before")
    @classmethod
    def revision_not_bool(cls, value):
        return _reject_bool_revision(value)


class DeleteTargetRequest(StrictModel):
    expected_revision: int = Field(ge=0)

    @field_validator("expected_revision", mode="before")
    @classmethod
    def revision_not_bool(cls, value):
        return _reject_bool_revision(value)


class ApprovalRequest(StrictModel):
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approve: bool


class PatchRequest(StrictModel):
    investigation_id: str


class ReconciliationConfirmRequest(StrictModel):
    """Operator confirmation binds only to the exact reviewed record.

    No caller-supplied evidence, container-state hashes, allowed actions,
    command strings or success status is accepted.
    """

    review_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    attestation: str = Field(min_length=1, max_length=500)
    operator_note: str = Field(min_length=1, max_length=500)


class PatchProfile(StrictModel):
    image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    test_paths: list[str] = Field(min_length=1, max_length=12)
    settings_module: str | None = Field(default=None, pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]{0,100}$")
    timeout_seconds: int = Field(default=180, ge=10, le=300)
    memory_mib: int = Field(default=512, ge=256, le=2048)
    scratch_dirs: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("scratch_dirs")
    @classmethod
    def scratch_scope(cls, paths):
        import re
        if len(set(paths)) != len(paths) or any(
            not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}", path) for path in paths
        ):
            raise ValueError("Scratch directories must be unique plain top-level names")
        return paths

    @field_validator("test_paths")
    @classmethod
    def test_scope(cls, paths):
        from pathlib import PurePosixPath
        for path in paths:
            parts = PurePosixPath(path)
            if not path or path.startswith("-") or parts.is_absolute() or ".." in parts.parts or "\\" in path or ":" in path:
                raise ValueError("Trusted test paths must be relative paths, not options")
        return paths


class DownloadCheck(ProbeSpec):
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_bytes: int = Field(gt=0, le=1024 * 1024 * 1024)
    deadline_seconds: int = Field(default=120, ge=5, le=180)


class OpsError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


TargetSpec.model_rebuild()
