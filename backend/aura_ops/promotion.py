"""Promote a verified deployment's bookkeeping without changing its container.

The upstream revision remains an ancestry anchor. A hash-bound overlay explicitly
identifies the local correction; it is never represented as a new upstream commit.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from .actions import _canonical, _fresh, _hash, _identity, _spec_hash
from .docker import identity_of
from .models import OpsError, utc_now
from .patches import _REGRESSION, _path, _read_file, _hash as file_hash, _source_files
from .source import SourceIndex
from .target_management import check_target_generation


async def promote(manager, spec, proposal_id):
    store = manager.store
    previous = store.get_setting("promotion." + proposal_id)
    if previous:
        _prev_proposal = store.get_proposal(proposal_id)
        if _prev_proposal is not None:
            check_target_generation(store, spec.target_id, _prev_proposal.get("investigation_id"))
        # A repeat request cannot revert later policy/source/baseline changes.
        if previous["new_spec_hash"] != _spec_hash(spec) \
                or previous["snapshot_hash"] != _hash(store.get_setting("snapshot." + spec.target_id)):
            raise OpsError("promotion_state_changed", "This deployment was promoted, but enrollment has since changed.", 409)
        if identity_of(await manager._container(spec)) != previous["identity"]:
            raise OpsError("target_changed", "Application identity changed after baseline promotion.", 409)
        await manager.actions._check_snapshot(spec, {"snapshot_hash": previous["snapshot_hash"]})
        await asyncio.to_thread(_source_files, spec, SourceIndex(spec))
        return previous

    proposal = store.get_proposal(proposal_id)
    if not proposal:
        raise OpsError("proposal_not_found", "Deployment proposal was not found.", 404)
    proposal = manager._approved(proposal, spec)
    investigation = store.get_investigation(proposal["investigation_id"])
    verification = proposal.get("verification") or {}
    expected = proposal.get("deployment_identity")
    if proposal.get("status") != "verified" or verification.get("verified") is not True \
            or verification.get("root_cause_fixed") is not True \
            or (verification.get("functional") or {}).get("verified") is not True \
            or not investigation or investigation.get("phase") != "verified_fixed" \
            or investigation.get("proposal_id") != proposal_id \
            or not expected or expected.get("image_id") != proposal["candidate_image_id"]:
        raise OpsError("verified_deployment_required", "Only a functionally verified approved deployment can become a baseline.", 409)
    target = store.get_target(spec.target_id)
    old_snapshot = store.get_setting("snapshot." + spec.target_id)
    checked, base, candidate, _ = await manager._revalidate(proposal, spec)
    if identity_of(await manager._container(spec)) != expected:
        raise OpsError("target_changed", "Only the verified deployed container can become the baseline.", 409)

    # Store a distinct source tree; never edit original source or patch evidence.
    directory = store.root / "versions" / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    if directory.resolve() != directory or directory.parent.is_symlink():
        raise OpsError("promotion_path_invalid", "Version files must stay inside the operations store.", 409)
    source_root = directory / "source"
    source_root.mkdir()
    patch = checked["record"]
    source_files = {}
    for name, digest in patch["candidate_manifest"].items():
        content = _read_file(_path(Path(patch["candidate_path"]), name))
        if file_hash(content) != digest:
            raise OpsError("deployment_patch_changed", "Candidate files changed during promotion.", 409)
        if name == _REGRESSION:
            # Keep previously proven tests in all future validation suites, outside
            # the reserved directory used to generate the next regression.
            name = "aura_verified_tests/test_" + proposal["patch_id"] + ".py"
        destination = _path(source_root, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        source_files[name] = digest
    lineage = {"proposal_id": proposal_id, "patch_id": proposal["patch_id"],
               "image_id": candidate["Id"], "base_image_id": base["Id"],
               "patch_metadata_sha256": proposal["patch_metadata_sha256"],
               "upstream_revision": spec.source_revision}
    manifest = {"source_dir": str(source_root), "revision": spec.source_revision,
                "files": source_files, "verified_overlay": lineage}
    manifest_bytes = _canonical(manifest)
    (directory / "source-manifest.json").write_bytes(manifest_bytes)

    # Compose includes keep original relative mounts, env and all network/volume
    # definitions. No resolved environment (which may contain secrets) is copied.
    override = directory / "deployment.json"
    override.write_bytes(_canonical({"services": {spec.compose_service: {
        "image": candidate["Id"], **manager._launch(spec)}}}))
    original = old_snapshot["compose"]
    compose = directory / "compose.json"
    compose.write_bytes(_canonical({"include": [{
        "path": [original["compose_file"], str(override)],
        "project_directory": str(Path(original["compose_file"]).parent),
        "env_file": original["env_file"] if original["env_sha256"] else str(manager.actions.empty_env),
    }]}))
    prepared_hashes = {path: file_hash(path.read_bytes()) for path in
                       (override, compose, directory / "source-manifest.json")}
    updated = spec.model_copy(deep=True)
    updated.source_dir = str(source_root)
    updated.source_manifest_sha256 = file_hash(manifest_bytes)
    updated.source_image_id = candidate["Id"]
    updated.compose_file = str(compose)
    regression_dir = "aura_verified_tests"
    if regression_dir not in updated.patch_profile.test_paths:
        updated.patch_profile.test_paths.append(regression_dir)
    updated = type(spec).model_validate(updated.model_dump())
    await asyncio.to_thread(_source_files, updated, SourceIndex(updated))
    observation = await manager.docker_factory(updated, runner=manager.runner).collect()
    _fresh(observation)
    if _identity(observation) != expected or observation.get("collection_errors") or observation.get("error"):
        raise OpsError("target_changed", "Fresh complete observations of the verified deployment are required.", 409)
    snapshot = await manager.actions.capture_snapshot(updated, observation, persist=False)
    # Recheck all original authority and files after asynchronous I/O. Durable
    # enrollment, baseline and audit are then committed in one CAS transaction.
    await manager._snapshot(spec, proposal["snapshot_hash"])
    if identity_of(await manager._container(updated)) != expected:
        raise OpsError("target_changed", "Application changed before promotion commit.", 409)
    manager._approved(proposal, spec)
    if any(file_hash(path.read_bytes()) != digest for path, digest in prepared_hashes.items()):
        raise OpsError("config_changed", "Prepared version metadata changed during promotion.", 409)
    if manager.actions._compose_files(updated) != {
            key: snapshot["compose"][key] for key in manager.actions._compose_files(updated)}:
        raise OpsError("config_changed", "Promoted configuration changed during capture.", 409)
    await asyncio.to_thread(_source_files, updated, SourceIndex(updated))
    record = {"proposal_id": proposal_id, "target_id": spec.target_id, "timestamp": utc_now(),
              "identity": expected, "lineage": lineage,
              "previous_target": target, "previous_snapshot": old_snapshot,
              "new_spec_hash": _spec_hash(updated), "snapshot_hash": _hash(snapshot),
              "source_manifest_sha256": updated.source_manifest_sha256,
              "container_mutated": False}
    return store.promote_baseline(target, old_snapshot, proposal, updated.model_dump(mode="json"), snapshot, record)
