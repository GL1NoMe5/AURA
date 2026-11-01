"""Build reviewed Python overlays and deploy them only with bound user approval.

Image creation never changes the target. The deployment result is deliberately
unverified: the service must check real operations before declaring a fix.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import uuid

from .actions import ActionExecutor, _canonical, _check_reconciliation, _fresh, _hash, _identity, _spec_hash
from .docker import CommandResult, DockerTarget, identity_of, run_command
from .models import ActionKind, OpsError, TargetSpec, utc_now
from .patches import (_REGRESSION, _assert_workspace,
                      _digest, _editable, _path, _read_file, _source_files)
from .source import SourceIndex
from .target_management import check_target_generation


_IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
_SAFE_PY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,290}\.py")
_BOUND = ("version", "action", "target_id", "investigation_id", "expected_identity",
          "spec_hash", "policy_hash", "enrollment_created_at", "snapshot_hash", "patch_id", "patch_metadata_id",
          "patch_metadata_sha256", "source_manifest_hash", "profile_hash", "candidate_manifest_hash",
          "edited_hashes", "regression_sha256", "base_image_id", "candidate_image_id",
          "deployment_root", "deployment_launch", "build_artifact_id", "build_artifact_sha256", "rollback_on_failure", "reason")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _binding(record: dict) -> str:
    if any(key not in record for key in _BOUND):
        raise OpsError("deployment_binding_missing", "Deployment proposal is incomplete.", 409)
    return _hash({key: record[key] for key in _BOUND})


def _root(spec: TargetSpec) -> PurePosixPath:
    value = getattr(spec, "deployment_root", None)
    root = PurePosixPath(value or "")
    if not value or not root.is_absolute() or root == PurePosixPath("/") \
            or root.as_posix() != value or ".." in root.parts \
            or any(part in ("", ".", "..") for part in value.split("/")[1:]):
        raise OpsError("deployment_mapping_required", "Enroll an exact absolute container application directory.", 409)
    return root


def _protected_edit(name: str, profile=None) -> bool:
    return bool(isinstance(name, str) and _SAFE_PY.fullmatch(name) and _editable(name, profile)
                and not any(part in ("", ".", "..") for part in name.split("/")))


def _safe_workspace(path: str, expected: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.resolve() != expected.resolve():
        raise OpsError("deployment_workspace_invalid", "Patch workspace is not the recorded private workspace.", 409)
    for part in (candidate, *candidate.parents):
        if part.is_symlink():
            raise OpsError("deployment_workspace_invalid", "Patch workspace cannot contain symlinks.", 409)
        if part == expected.parent.parent.parent:
            break
    if not candidate.is_dir():
        raise OpsError("deployment_workspace_missing", "The validated patch workspace is missing.", 409)
    return candidate


def _check_runner_command(command, suffix: str, attempt: int, paths: list,
                          profile_hash: str, manifest_hash: str) -> str:
    """Require the exact owned invocation and return its nonce for report binding."""
    invalid = OpsError("deployment_tests_missing",
                       "Stored validation does not establish all required test outcomes.", 409)
    if not isinstance(command, list) or len(command) != 16 \
            or not all(isinstance(value, str) for value in command):
        raise invalid
    nonce = command[7]
    if not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise invalid
    expected = ["python", "/aura/test_runner.py", "--stage", suffix,
                "--attempt", str(attempt), "--nonce", nonce,
                "--expected-paths", json.dumps(list(paths), separators=(",", ":")),
                "--profile-hash", profile_hash, "--manifest-hash", manifest_hash,
                "--rootdir", "/workspace"]
    if command != expected:
        raise invalid
    return nonce


class DeploymentManager:
    def __init__(self, store, docker_factory=DockerTarget, runner=run_command):
        self.store, self.docker_factory, self.runner = store, docker_factory, runner
        self.actions = ActionExecutor(store, docker_factory, runner)
        self.builds = store.root / "builds"
        self.builds.mkdir(exist_ok=True)
        if self.builds.is_symlink() or self.builds.resolve().parent != store.root:
            raise OpsError("deployment_workspace_invalid", "Builds must remain inside the operations store.", 409)

    async def _command(self, spec, *args, timeout=15):
        return await self.runner(["docker", "--context", spec.docker_context, *args],
                                 timeout=timeout, max_stdout=1024 * 1024, max_stderr=65536)

    def _registered(self, spec, *, created_at=None):
        """Re-read authority immediately before dispatch, never cached enrollment."""
        record = self.store.get_target(spec.target_id)
        if record is None or record.get("monitoring") is not True:
            raise OpsError("target_disconnected", "The target is no longer enrolled for active operations.", 409)
        try:
            current = TargetSpec.model_validate({key: value for key, value in record.items()
                                                 if key in TargetSpec.model_fields})
        except ValueError as exc:
            raise OpsError("policy_changed", "Target enrollment is no longer valid.", 409) from exc
        if _spec_hash(current) != _spec_hash(spec) \
                or (created_at is not None and record.get("created_at") != created_at):
            raise OpsError("policy_changed", "Target registration or policy changed during the operation.", 409)
        return record

    @staticmethod
    def _launch(spec):
        if spec.deployment_launch is None:
            raise OpsError("deployment_launch_required", "Enroll a reviewed direct server launch that performs no database migrations before deployment.", 409)
        return spec.deployment_launch.model_dump()

    def _artifact(self, supplied: dict) -> tuple[dict, bytes]:
        if not isinstance(supplied, dict) or not supplied.get("artifact_id"):
            raise OpsError("deployment_artifact_missing", "A durable verified artifact is required.", 409)
        stored = self.store.get_artifact(supplied["artifact_id"])
        if stored is None or any(supplied.get(key) != stored[key] for key in ("sha256", "size")):
            raise OpsError("deployment_artifact_changed", "Artifact metadata no longer matches stored evidence.", 409)
        return stored, Path(stored["path"]).read_bytes()

    def _validate_patch(self, spec, investigation_id, patch) -> dict:
        if not spec.source_dir or not spec.source_revision or not spec.source_manifest_sha256 or not spec.patch_profile:
            raise OpsError("deployment_source_unverified", "Verified source provenance and test profile are required.", 409)
        metadata, content = self._artifact(patch.get("metadata_artifact"))
        try:
            trusted = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise OpsError("deployment_artifact_invalid", "Patch metadata is not valid JSON.", 409) from exc
        if not isinstance(trusted, dict) or _canonical(trusted) != _canonical({
                key: value for key, value in patch.items() if key != "metadata_artifact"}):
            raise OpsError("deployment_patch_forged", "Patch state differs from its durable validation result.", 409)
        if trusted.get("status") != "tests_passed" or trusted.get("target_id") != spec.target_id \
                or trusted.get("investigation_id") != investigation_id \
                or trusted.get("source_revision") != spec.source_revision:
            raise OpsError("deployment_patch_unverified", "This target requires a matching successfully tested patch.", 409)
        patch_id = trusted.get("patch_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", patch_id) or metadata["name"] != f"{patch_id}.json":
            raise OpsError("deployment_patch_unverified", "Patch metadata identity is invalid.", 409)
        if trusted.get("profile_hash") != _digest(spec.patch_profile.model_dump()) \
                or trusted.get("profile") != spec.patch_profile.model_dump():
            raise OpsError("deployment_profile_changed", "The trusted test profile changed after validation.", 409)
        attempts = trusted.get("attempts", [])
        successful = [attempt for attempt in attempts if attempt.get("status") == "tests_passed"]
        if len(successful) != 1 or successful[0] is not attempts[-1] \
                or type(successful[0].get("attempt")) is not int \
                or successful[0].get("attempt") not in (1, 2):
            raise OpsError("deployment_tests_missing", "Successful before/after/original-suite evidence is required.", 409)
        attempt = successful[0]
        tests = attempt.get("tests") or []
        if len(tests) != 3:
            raise OpsError("deployment_tests_missing", "All three validation stages must be present.", 409)
        collection = attempt.get("baseline_collection")
        pending: list[tuple[dict, str, list]] = [(collection, "collect", list(spec.patch_profile.test_paths))]
        for index, suffix in enumerate(("before", "after", "original-suite")):
            paths = list(spec.patch_profile.test_paths) if index == 2 else [_REGRESSION]
            pending.append((tests[index], suffix, paths))
        # Structural presence first (log-only candidates fail explicitly here);
        # report contents are bound after workspaces are verified below.
        for entry, suffix, paths in pending:
            if not isinstance(entry, dict) or not isinstance(entry.get("report"), dict):
                raise OpsError("deployment_protocol_missing",
                               "Structured test reports are required; log-only candidates must be freshly validated.", 409)
            report_artifact, _ = self._artifact(entry.get("report"))
            if report_artifact["name"] != f"{patch_id}-{attempt['attempt']}-{suffix}.report.json":
                raise OpsError("deployment_tests_missing", "Stored validation does not establish all required test outcomes.", 409)
            log_artifact, _ = self._artifact(entry.get("artifact"))
            if log_artifact["name"] != f"{patch_id}-{attempt['attempt']}-{suffix}.log":
                raise OpsError("deployment_tests_missing", "Stored validation does not establish all required test outcomes.", 409)
            if entry.get("stage") != f"{attempt['attempt']}-{suffix}" \
                    or not isinstance(entry.get("exit_code"), int) or isinstance(entry.get("exit_code"), bool):
                raise OpsError("deployment_tests_missing", "Stored validation does not establish all required test outcomes.", 409)
        source = SourceIndex(spec)
        hashes, payloads = _source_files(spec, source)
        if _digest(hashes) != trusted.get("source_manifest_hash"):
            raise OpsError("deployment_source_changed", "Enrolled source changed since patch validation.", 409)
        parent = self.store.root / "workspaces" / patch_id
        before = _safe_workspace(trusted.get("baseline_path", ""), parent / f"attempt-{attempt['attempt']}-before")
        after = _safe_workspace(trusted.get("candidate_path", ""), parent / f"attempt-{attempt['attempt']}-candidate")
        manifests = trusted.get("candidate_manifest")
        edited = trusted.get("edited_hashes")
        edits = trusted.get("edits")
        if not isinstance(manifests, dict) or not isinstance(edited, dict) or not 1 <= len(edited) <= 3 \
                or not isinstance(edits, list) or len(edits) != len(edited):
            raise OpsError("deployment_manifest_invalid", "Reviewed edits and candidate inventory are required.", 409)
        expected_names = set(payloads) | {_REGRESSION}
        if set(manifests) != expected_names:
            raise OpsError("deployment_manifest_invalid", "Candidate file inventory differs from the validated source.", 409)
        regression = _read_file(_path(after, _REGRESSION))
        if _sha(regression) != trusted.get("regression_sha256") \
                or _read_file(_path(before, _REGRESSION)) != regression:
            raise OpsError("deployment_regression_changed", "The before/after regression test changed.", 409)
        before_hashes = {name: _sha(value) for name, value in payloads.items()}
        before_hashes[_REGRESSION] = _sha(regression)
        expected_candidate = dict(before_hashes)
        changes, diff = {}, ""
        seen = set()
        for edit in edits:
            name = edit.get("path")
            if name in seen or name not in edited or name not in payloads or not _protected_edit(name, spec.patch_profile):
                raise OpsError("deployment_edit_forbidden", "Only reviewed application Python files may enter an image.", 409)
            seen.add(name)
            original = payloads[name].decode("utf-8")
            old, new = edit.get("old"), edit.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str) or original.count(old) != 1:
                raise OpsError("deployment_edit_changed", "Recorded edit no longer applies uniquely.", 409)
            candidate = original.replace(old, new, 1).encode("utf-8")
            if edited[name] != {"before": hashes[name], "after": _sha(candidate)}:
                raise OpsError("deployment_edit_changed", "Recorded edit hashes do not match the approved content.", 409)
            if _read_file(_path(after, name)) != candidate:
                raise OpsError("deployment_edit_changed", "Candidate source was modified after validation.", 409)
            changes[name] = candidate
            expected_candidate[name] = _sha(candidate)
            diff += "".join(difflib.unified_diff(original.splitlines(keepends=True),
                                                candidate.decode().splitlines(keepends=True),
                                                fromfile="a/" + name, tofile="b/" + name))
        if expected_candidate != manifests:
            raise OpsError("deployment_manifest_invalid", "Candidate changes extend beyond reviewed application edits.", 409)
        _assert_workspace(before, before_hashes, spec.patch_profile.scratch_dirs)
        _assert_workspace(after, expected_candidate, spec.patch_profile.scratch_dirs)
        diff += "".join(difflib.unified_diff([], regression.decode().splitlines(keepends=True),
                                            fromfile="/dev/null", tofile="b/" + _REGRESSION))
        diff_artifact, stored_diff = self._artifact(trusted.get("artifact"))
        if diff.encode() != stored_diff or diff_artifact["name"] != f"{patch_id}.patch":
            raise OpsError("deployment_diff_changed", "Review diff does not match the validated edits.", 409)
        from .test_protocol import (check_after, check_before, check_collect, check_suite,
                                    parse_report, validate_structure)
        from .test_runner import runner_sha256
        profile_hash = _digest(spec.patch_profile.model_dump())
        expected_runner = runner_sha256()
        before_digest = _digest(before_hashes)
        candidate_digest = _digest(expected_candidate)
        validated: dict[str, tuple[list, dict, int]] = {}
        for entry, suffix, paths in pending:
            _, report_bytes = self._artifact(entry.get("report"))
            report = parse_report(report_bytes)
            digest = before_digest if suffix in ("collect", "before") else candidate_digest
            nonce = _check_runner_command(entry.get("command"), suffix, attempt["attempt"],
                                          paths, profile_hash, digest)
            collected, phases = validate_structure(
                report, stage=suffix, attempt=attempt["attempt"], expected_paths=list(paths),
                profile_hash=profile_hash, manifest_hash=digest,
                runner_hash=expected_runner, nonce=nonce, actual_exit=entry["exit_code"])
            validated[suffix] = (collected, phases, entry["exit_code"])
        baseline_nodes, baseline_phases, baseline_exit = validated["collect"]
        if not check_collect(baseline_nodes, baseline_phases, exit_code=baseline_exit):
            raise OpsError("deployment_tests_missing", "Stored validation does not establish all required test outcomes.", 409)
        before_nodes, before_phases, before_exit = validated["before"]
        if not check_before(before_nodes, before_phases, exit_code=before_exit):
            raise OpsError("deployment_tests_missing", "Stored validation does not establish all required test outcomes.", 409)
        after_nodes, after_phases, after_exit = validated["after"]
        if not check_after(after_nodes, after_phases, exit_code=after_exit, before_nodes=before_nodes):
            raise OpsError("deployment_tests_missing", "Stored validation does not establish all required test outcomes.", 409)
        suite_nodes, suite_phases, suite_exit = validated["original-suite"]
        if not check_suite(suite_nodes, suite_phases, exit_code=suite_exit, baseline_nodes=baseline_nodes):
            raise OpsError("deployment_tests_missing", "Stored validation does not establish all required test outcomes.", 409)
        return {"record": trusted, "metadata": metadata, "changes": changes,
                "candidate_manifest_hash": _digest(expected_candidate)}

    async def _image(self, spec, reference):
        result = await self._command(spec, "image", "inspect", reference)
        try:
            rows = json.loads(result.stdout)
            row = rows[0]
            valid = not result.returncode and len(rows) == 1 and _IMAGE.fullmatch(row["Id"]) and row.get("Os") == "linux"
        except (ValueError, TypeError, KeyError, IndexError):
            valid = False
        if not valid:
            raise OpsError("deployment_image_unavailable", "The exact local Linux image could not be verified.", 409)
        return row

    async def _container(self, spec):
        result = await self._command(spec, "container", "inspect", spec.container_name)
        try:
            rows = json.loads(result.stdout)
            row = rows[0]
            if result.returncode or len(rows) != 1:
                raise ValueError("container")
            identity_of(row)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise OpsError("deployment_target_unavailable", "The enrolled container could not be identified.", 409) from exc
        return row

    @staticmethod
    def _check_mounts(spec, container, image, names):
        root = _root(spec)
        destinations = [root / name for name in names]
        mounts = [entry.get("Destination") for entry in container.get("Mounts", [])]
        mounts += list((image.get("Config") or {}).get("Volumes") or {})
        for raw in mounts:
            if not isinstance(raw, str) or not raw.startswith("/"):
                raise OpsError("deployment_mount_unknown", "Cannot determine whether a runtime mount hides edited code.", 409)
            mount = PurePosixPath(raw)
            if any(path == mount or mount in path.parents for path in destinations):
                raise OpsError("deployment_mount_conflict", "A runtime volume would hide the reviewed Python changes.", 409)

    @staticmethod
    def _base_provenance(spec, image):
        config = image.get("Config") or {}
        labels = config.get("Labels") or {}
        if config.get("OnBuild"):
            raise OpsError("deployment_onbuild_forbidden", "Base images with ONBUILD instructions cannot be used for reviewed overlays.", 409)
        if image["Id"] != spec.source_image_id or labels.get("org.opencontainers.image.revision") != spec.source_revision:
            raise OpsError("deployment_source_image_mismatch", "The base image must match the enrolled immutable image and source revision.", 409)
        layers = (image.get("RootFS") or {}).get("Layers")
        if not isinstance(layers, list) or not layers or any(not _IMAGE.fullmatch(str(layer)) for layer in layers):
            raise OpsError("deployment_image_unverified", "Base filesystem layers are required to verify inheritance.", 409)

    @staticmethod
    def _check_candidate(base, candidate, patch_sha, patch_id):
        old = dict(base.get("Config") or {})
        new = dict(candidate.get("Config") or {})
        old_labels = old.pop("Labels", None) or {}
        new_labels = new.pop("Labels", None) or {}
        expected_labels = {**old_labels, "aura.patch.sha256": patch_sha,
                           "aura.patch.id": patch_id, "aura.patch.base": base["Id"]}
        old_layers = (base.get("RootFS") or {}).get("Layers") or []
        new_layers = (candidate.get("RootFS") or {}).get("Layers") or []
        if old != new or new_labels != expected_labels or not old_layers \
                or new_layers[:len(old_layers)] != old_layers or len(new_layers) <= len(old_layers):
            raise OpsError("deployment_image_changed", "Candidate image did not preserve the approved base filesystem and runtime configuration.", 409)

    async def _snapshot(self, spec, snapshot_hash):
        snapshot = await self.actions._check_snapshot(spec, {"snapshot_hash": snapshot_hash})
        if snapshot["identity"]["image_id"] != spec.source_image_id \
                or snapshot.get("source_revision") != spec.source_revision \
                or snapshot.get("source_dir") != spec.source_dir:
            raise OpsError("deployment_baseline_mismatch", "Healthy baseline must refer to the enrolled source and exact base image.", 409)
        return snapshot

    async def _compose_mounts(self, spec, snapshot, names):
        # Configuration replay can reintroduce a mount removed by an external
        # override. Inspect the approved base as well as the current container.
        result = await self.runner(
            self.actions._compose_argv(spec, snapshot["compose"]) + ["config", "--format", "json"],
            timeout=15, max_stdout=1024 * 1024, max_stderr=65536)
        try:
            service = json.loads(result.stdout)["services"][spec.compose_service]
            if result.returncode:
                raise ValueError("compose")
            if service.get("post_start") or service.get("pre_stop"):
                raise OpsError("deployment_lifecycle_unsupported", "Deployment cannot replay Compose lifecycle hooks; enroll a hook-free server profile.", 409)
            mounts = []
            for item in service.get("volumes") or []:
                if not isinstance(item, dict) or not isinstance(item.get("target"), str):
                    raise ValueError("mount")
                mounts.append({"Destination": item["target"]})
            for kind in ("configs", "secrets"):
                for item in service.get(kind) or []:
                    if not isinstance(item, dict) or not isinstance(item.get("source"), str):
                        raise ValueError("config mount")
                    destination = item.get("target") or ("/run/secrets/" if kind == "secrets" else "/") + item["source"]
                    mounts.append({"Destination": destination})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OpsError("deployment_mount_unknown", "Approved Compose mount destinations could not be verified.", 409) from exc
        self._check_mounts(spec, {"Mounts": mounts}, {}, names)

    async def build_proposal(self, spec: TargetSpec, investigation_id: str, patch: dict, observation: dict) -> dict:
        _check_reconciliation(self.store, spec.target_id)
        check_target_generation(self.store, spec.target_id, investigation_id)
        registered = self._registered(spec)
        self._launch(spec)
        _fresh(observation)
        expected = _identity(observation)
        root = _root(spec)
        if not spec.source_image_id or expected["image_id"] != spec.source_image_id:
            raise OpsError("deployment_source_image_mismatch", "Enroll the currently deployed immutable image as the source image.", 409)
        checked = await asyncio.to_thread(self._validate_patch, spec, investigation_id, patch)
        check_target_generation(self.store, spec.target_id, investigation_id)
        snapshot = self.store.get_setting(f"snapshot.{spec.target_id}")
        if not snapshot:
            raise OpsError("snapshot_required", "Capture a healthy baseline before building a deployment.", 409)
        snapshot_hash = _hash(snapshot)
        snapshot = await self._snapshot(spec, snapshot_hash)
        await self._compose_mounts(spec, snapshot, checked["changes"])
        container = await self._container(spec)
        if identity_of(container) != expected:
            raise OpsError("target_changed", "Target identity changed before building the reviewed image.", 409)
        base = await self._image(spec, spec.source_image_id)
        self._base_provenance(spec, base)
        self._check_mounts(spec, container, base, checked["changes"])
        build_id = uuid.uuid4().hex
        context = self.builds / build_id
        context.mkdir(exist_ok=False)
        base_tag, candidate_tag = f"aura-reviewed-base:{build_id}", f"aura-reviewed-patch:{build_id}"
        self._registered(spec, created_at=registered["created_at"])
        tagged = await self._command(spec, "image", "tag", base["Id"], base_tag)
        if tagged.returncode or (await self._image(spec, base_tag))["Id"] != base["Id"]:
            raise OpsError("deployment_base_tag_changed", "Cannot bind the private build tag to the approved image.", 409)
        dockerfile = [f"FROM {base_tag}"]
        file_hashes = {}
        for name, content in checked["changes"].items():
            destination = _path(context, "payload/" + name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            file_hashes["payload/" + name] = _sha(content)
            dockerfile.append("COPY " + json.dumps(["payload/" + name, str(root / name)]))
        labels = {"aura.patch.sha256": checked["metadata"]["sha256"],
                  "aura.patch.id": checked["record"]["patch_id"], "aura.patch.base": base["Id"]}
        dockerfile.append("LABEL " + " ".join(f"{key}={json.dumps(value)}" for key, value in labels.items()))
        dockerfile_bytes = ("\n".join(dockerfile) + "\n").encode()
        (context / "Dockerfile").write_bytes(dockerfile_bytes)
        file_hashes["Dockerfile"] = _sha(dockerfile_bytes)
        if (await self._image(spec, base_tag))["Id"] != base["Id"]:
            raise OpsError("deployment_base_tag_changed", "The private base image tag changed before build.", 409)
        self._registered(spec, created_at=registered["created_at"])
        result = await self._command(spec, "build", "--network", "none", "--pull=false", "--tag", candidate_tag,
                                     "--file", str(context / "Dockerfile"), str(context), timeout=120)
        if result.returncode:
            raise OpsError("deployment_build_failed", "Reviewed image build failed; target was not changed.", 409)
        candidate = await self._image(spec, candidate_tag)
        self._check_candidate(base, candidate, checked["metadata"]["sha256"], checked["record"]["patch_id"])
        self._check_context(context, file_hashes)
        refreshed = await asyncio.to_thread(self._validate_patch, spec, investigation_id, patch)
        if refreshed["metadata"]["sha256"] != checked["metadata"]["sha256"]:
            raise OpsError("deployment_patch_changed", "Patch provenance changed during build.", 409)
        if identity_of(await self._container(spec)) != expected:
            raise OpsError("target_changed", "Target changed during image build; create a new proposal.", 409)
        await self._snapshot(spec, snapshot_hash)
        self._registered(spec, created_at=registered["created_at"])
        _check_reconciliation(self.store, spec.target_id)
        build_record = {"build_id": build_id, "context": str(context), "files": file_hashes,
                        "base_image_id": base["Id"], "candidate_image_id": candidate["Id"],
                        "base_tag": base_tag, "candidate_tag": candidate_tag,
                        "base_config_hash": _hash(base["Config"]), "candidate_config_hash": _hash(candidate["Config"]),
                        "base_rootfs_hash": _hash(base["RootFS"]), "candidate_rootfs_hash": _hash(candidate["RootFS"]),
                        "patch_metadata_sha256": checked["metadata"]["sha256"], "created_at": utc_now()}
        artifact = self.store.save_artifact(f"deployment-{build_id}.json", _canonical(build_record), "application/json")
        record = checked["record"]
        proposal = {"version": 1, "action": ActionKind.DEPLOY.value, "target_id": spec.target_id,
                    "investigation_id": investigation_id, "expected_identity": expected,
                    "spec_hash": _spec_hash(spec), "policy_hash": _hash(spec.policy.model_dump(mode="json")),
                    "enrollment_created_at": registered["created_at"],
                    "snapshot_hash": snapshot_hash, "patch_id": record["patch_id"],
                    "patch_metadata_id": checked["metadata"]["artifact_id"],
                    "patch_metadata_sha256": checked["metadata"]["sha256"],
                    "source_manifest_hash": record["source_manifest_hash"], "profile_hash": record["profile_hash"],
                    "candidate_manifest_hash": checked["candidate_manifest_hash"],
                    "edited_hashes": record["edited_hashes"], "regression_sha256": record["regression_sha256"],
                    "base_image_id": base["Id"], "candidate_image_id": candidate["Id"],
                    "deployment_root": str(root), "build_artifact_id": artifact["artifact_id"],
                    "deployment_launch": self._launch(spec),
                    "build_artifact_sha256": artifact["sha256"], "rollback_on_failure": True,
                    "reason": "Deploy the reviewed, tested Python correction; restore the exact baseline image if independent verification fails.",
                    "automation_eligible": False, "blocked_reason": None}
        proposal["proposal_hash"] = _binding(proposal)
        return self.store.put_proposal(proposal)

    @staticmethod
    def _check_context(context, hashes):
        if context.is_symlink():
            raise OpsError("deployment_context_changed", "Build context cannot be a symlink.", 409)
        actual = set()
        for path in context.rglob("*"):
            if path.is_symlink():
                raise OpsError("deployment_context_changed", "Build context contains a symlink.", 409)
            if path.is_file():
                name = path.relative_to(context).as_posix()
                actual.add(name)
                if name not in hashes or _sha(_read_file(path)) != hashes[name]:
                    raise OpsError("deployment_context_changed", "Reviewed build context changed.", 409)
        if actual != set(hashes):
            raise OpsError("deployment_context_changed", "Reviewed build context is incomplete.", 409)

    def _approved(self, proposal, spec):
        stored = self.store.get_proposal(proposal.get("proposal_id", ""))
        if not stored or _binding(proposal) != proposal.get("proposal_hash") \
                or stored.get("proposal_hash") != proposal.get("proposal_hash") \
                or _binding(stored) != stored.get("proposal_hash"):
            raise OpsError("proposal_changed", "Deployment proposal no longer matches its durable binding.", 409)
        if stored.get("authorization") != "user" or stored.get("approved_hash") != stored["proposal_hash"]:
            raise OpsError("deployment_approval_required", "Deployment requires explicit approval of this exact proposal.", 403)
        if stored.get("action") != ActionKind.DEPLOY.value or stored.get("version") != 1:
            raise OpsError("deployment_binding_missing", "Only a versioned deployment proposal can use this executor.", 409)
        if stored["target_id"] != spec.target_id or stored["spec_hash"] != _spec_hash(spec) \
                or stored["policy_hash"] != _hash(spec.policy.model_dump(mode="json")) \
                or stored["deployment_launch"] != self._launch(spec):
            raise OpsError("policy_changed", "Target enrollment or policy changed after approval.", 409)
        self._registered(spec, created_at=stored["enrollment_created_at"])
        check_target_generation(self.store, spec.target_id, stored.get("investigation_id"))
        return stored

    async def _revalidate(self, proposal, spec):
        metadata = self.store.get_artifact(proposal["patch_metadata_id"])
        if not metadata or metadata["sha256"] != proposal["patch_metadata_sha256"]:
            raise OpsError("deployment_patch_changed", "Patch metadata changed after approval.", 409)
        patch = json.loads(Path(metadata["path"]).read_bytes())
        patch["metadata_artifact"] = {key: value for key, value in metadata.items() if key != "path"}
        checked = await asyncio.to_thread(self._validate_patch, spec, proposal["investigation_id"], patch)
        for key in ("source_manifest_hash", "profile_hash", "edited_hashes", "regression_sha256"):
            if checked["record"][key] != proposal[key]:
                raise OpsError("deployment_patch_changed", "Reviewed source/test binding changed.", 409)
        if checked["candidate_manifest_hash"] != proposal["candidate_manifest_hash"]:
            raise OpsError("deployment_patch_changed", "Reviewed candidate inventory changed.", 409)
        build_artifact = self.store.get_artifact(proposal["build_artifact_id"])
        if not build_artifact or build_artifact["sha256"] != proposal["build_artifact_sha256"]:
            raise OpsError("deployment_build_changed", "Reviewed build provenance changed.", 409)
        build = json.loads(Path(build_artifact["path"]).read_bytes())
        if not re.fullmatch(r"[a-f0-9]{32}", build.get("build_id", "")):
            raise OpsError("deployment_build_changed", "Build identity is invalid.", 409)
        context = Path(build["context"])
        if context.resolve() != self.builds / build["build_id"]:
            raise OpsError("deployment_build_changed", "Build context moved outside the private workspace.", 409)
        self._check_context(context, build["files"])
        base = await self._image(spec, proposal["base_image_id"])
        candidate = await self._image(spec, proposal["candidate_image_id"])
        self._base_provenance(spec, base)
        self._check_candidate(base, candidate, proposal["patch_metadata_sha256"], proposal["patch_id"])
        for label, image in (("base", base), ("candidate", candidate)):
            if build[f"{label}_image_id"] != image["Id"] \
                    or build[f"{label}_config_hash"] != _hash(image["Config"]) \
                    or build[f"{label}_rootfs_hash"] != _hash(image["RootFS"]):
                raise OpsError("deployment_image_changed", "Image configuration or filesystem differs from the reviewed build.", 409)
        snapshot = await self._snapshot(spec, proposal["snapshot_hash"])
        await self._compose_mounts(spec, snapshot, checked["changes"])
        check_target_generation(self.store, spec.target_id, proposal.get("investigation_id"))
        return checked, base, candidate, snapshot

    async def execute(self, proposal, spec, cancel_event):
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        _check_reconciliation(self.store, spec.target_id)
        stored = self._approved(proposal, spec)
        if stored.get("status") != "executing" or stored.get("deployment_dispatched"):
            raise OpsError("action_not_claimed", "Deployment must be claimed exactly once before execution.", 409)
        checked, base, candidate, snapshot = await self._revalidate(stored, spec)
        container = await self._container(spec)
        if identity_of(container) != stored["expected_identity"]:
            raise OpsError("target_changed", "The deployment target changed after approval.", 409)
        self._check_mounts(spec, container, base, checked["changes"])
        return await self._replay(stored, spec, snapshot, candidate["Id"], cancel_event, rollback=False,
                                  expected=stored["expected_identity"])

    async def rollback(self, proposal, spec, cancel_event):
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        _check_reconciliation(self.store, spec.target_id)
        stored = self._approved(proposal, spec)
        if not stored.get("deployment_dispatched") or stored.get("rollback_on_failure") is not True \
                or stored.get("rollback_attempted_at") or not stored.get("deployment_identity") \
                or stored.get("status") != "verification_failed":
            raise OpsError("rollback_not_authorized", "This deployment has no unused, known-outcome rollback authorization.", 409)
        checked, base, candidate, snapshot = await self._revalidate(stored, spec)
        container = await self._container(spec)
        current = identity_of(container)
        if current != stored["deployment_identity"] or current["image_id"] != candidate["Id"]:
            raise OpsError("target_changed", "Rollback only applies to the exact container created by this deployment.", 409)
        self._check_mounts(spec, container, candidate, checked["changes"])
        return await self._replay(stored, spec, snapshot, base["Id"], cancel_event, rollback=True, expected=current)

    async def _replay(self, proposal, spec, snapshot, image_id, cancel_event, *, rollback, expected):
        override = self.builds / f"replay-{uuid.uuid4().hex}.json"
        # Both rollout and rollback bypass the inherited startup script. A
        # healthy baseline has already initialized the persistent application.
        override.write_bytes(_canonical({"services": {spec.compose_service: {
            "image": image_id, **self._launch(spec)}}}))
        dispatched = False
        async def perform():
            nonlocal dispatched
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            if identity_of(await self._container(spec)) != expected:
                raise OpsError("target_changed", "Container changed immediately before deployment dispatch.", 409)
            await self._snapshot(spec, proposal["snapshot_hash"])
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            # No await between this final authority read, CAS claim and command
            # dispatch. Disconnect/revocation during earlier checks wins.
            fresh = self._approved(proposal, spec)
            if fresh.get("status") != proposal["status"]:
                raise OpsError("action_already_claimed", "Deployment was cancelled or claimed while preconditions were checked.", 409)
            if rollback and (fresh.get("rollback_attempted_at") or not fresh.get("deployment_dispatched")):
                raise OpsError("rollback_not_authorized", "Rollback authorization is no longer available.", 409)
            if not rollback and fresh.get("deployment_dispatched"):
                raise OpsError("action_already_claimed", "Deployment was already dispatched.", 409)
            _check_reconciliation(self.store, spec.target_id)
            transition = self.store.transition_proposal(
                proposal["proposal_id"], proposal["status"], "rolling_back" if rollback else "dispatching",
                **({"rollback_attempted_at": utc_now()} if rollback else
                   {"dispatch_attempted_at": utc_now(), "deployment_dispatched": True}))
            if transition is None:
                raise OpsError("action_already_claimed", "Deployment or rollback was already attempted or cancelled.", 409)
            dispatched = True
            argv = self.actions._compose_argv(spec, snapshot["compose"])
            argv += ["-f", str(override), "up", "-d", "--no-deps", "--no-build", "--pull", "never", spec.compose_service]
            result = await self.runner(argv, timeout=60, max_stdout=1024 * 1024, max_stderr=65536)
            if result.returncode:
                raise OpsError("action_outcome_unknown", "Compose did not establish a known result; reconcile before further action.", 409)
            after = identity_of(await self._container(spec))
            if after["image_id"] != image_id:
                raise OpsError("action_outcome_unknown", "The resulting image does not match the approved deployment.", 409)
            self.store.update_proposal(proposal["proposal_id"], **{
                "rollback_identity" if rollback else "deployment_identity": after})
            return {"action": "rollback_deployment" if rollback else ActionKind.DEPLOY.value,
                    "proposal_id": proposal["proposal_id"], "identity": after,
                    "command_completed": True, "verified": False,
                    "candidate_image_id": proposal["candidate_image_id"], "base_image_id": proposal["base_image_id"],
                    "timestamp": utc_now()}
        operation, cancellation = asyncio.create_task(perform()), asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({operation, cancellation}, return_when=asyncio.FIRST_COMPLETED)
            if cancellation in done and cancel_event.is_set():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                if dispatched:
                    raise OpsError("action_outcome_unknown", "Deployment interrupted after dispatch; inspect the target before retrying.", 409)
                raise asyncio.CancelledError()
            return await operation
        except asyncio.CancelledError as exc:
            if dispatched:
                raise OpsError("action_outcome_unknown", "Deployment interrupted after dispatch; inspect the target before retrying.", 409) from exc
            raise
        except Exception as exc:
            if dispatched and not (isinstance(exc, OpsError) and exc.code == "action_outcome_unknown"):
                raise OpsError("action_outcome_unknown", "Deployment was dispatched but its final state is uncertain.", 409) from exc
            raise
        finally:
            for task in (operation, cancellation):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, cancellation, return_exceptions=True)
            override.unlink(missing_ok=True)
