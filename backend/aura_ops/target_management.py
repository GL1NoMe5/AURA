"""Pure helpers for connection revisions and setup summaries.

No Docker, model, filesystem or network access happens here. The
configuration summary describes enrolled setup only; it never describes
permission, health, or the ability to execute an operation now.
"""
from __future__ import annotations

from .models import OpsError


def connection_revision(record) -> int:
    if not isinstance(record, dict):
        return 0
    value = record.get("connection_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def investigation_revision(row) -> int:
    if not isinstance(row, dict):
        return 0
    value = row.get("connection_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def revision_for_investigation(store, investigation_id) -> int:
    """Bound generation for an investigation; legacy/missing means 0."""
    if not investigation_id:
        return 0
    try:
        row = store.get_investigation(investigation_id)
    except Exception:
        return 0
    return investigation_revision(row)


def check_target_generation(store, target_id, investigation_id) -> None:
    """Reject work bound to an older connection profile.

    A->B->A editing increments the revision twice, so an old diagnosis
    cannot regain authority merely because its spec hash matches again.
    """
    record = store.get_target(target_id) if target_id else None
    if record is None:
        # A removed registration fails; a direct legacy executor call with
        # no enrollment row keeps its pre-revision behaviour at generation 0.
        try:
            retired = store.is_retired(target_id)
        except Exception:
            retired = False
        if retired:
            raise OpsError("target_not_found", "Application is not enrolled.", 404)
        return
    if revision_for_investigation(store, investigation_id) != connection_revision(record):
        raise OpsError(
            "target_configuration_changed",
            "The connection profile changed since this diagnosis; start a new investigation.",
            409,
        )


def _spec_dict(spec) -> dict:
    if spec is None:
        return {}
    if hasattr(spec, "model_dump"):
        return spec.model_dump(mode="json")
    return dict(spec)


def build_configuration(spec, snapshot) -> dict:
    """Setup-only summary computed from enrolled fields and baseline presence."""
    data = _spec_dict(spec)
    probes = data.get("probes") or []
    compose_ok = bool(data.get("compose_file") and data.get("compose_project")
                      and data.get("compose_service"))
    snapshot_ok = bool(isinstance(snapshot, dict) and snapshot.get("compose"))
    verified_source = bool(data.get("source_dir") and data.get("source_revision")
                           and data.get("source_manifest_sha256"))
    patch_ok = data.get("patch_profile") is not None
    root_ok = bool(data.get("deployment_root"))
    launch_ok = data.get("deployment_launch") is not None
    recovery_ok = bool(data.get("recovery_checks"))
    source_bound = bool(data.get("source_dir") or data.get("source_revision") or data.get("source_image_id"))

    def section(missing):
        return {"configured": not missing, "missing": list(missing)}

    monitoring_missing = [] if probes else ["probes"]
    restart_missing = [] if (data.get("container_name") and data.get("docker_context")) else ["container_identity"]
    recovery_missing = []
    if not compose_ok:
        recovery_missing.append("compose_profile")
    if not snapshot_ok:
        recovery_missing.append("known_good_snapshot")
    if source_bound and not launch_ok:
        recovery_missing.append("deployment_launch")
    code_fix_missing = []
    if not verified_source:
        code_fix_missing.append("verified_source")
    if not patch_ok:
        code_fix_missing.append("patch_profile")
    deployment_missing = []
    if not compose_ok:
        deployment_missing.append("compose_profile")
    if not snapshot_ok:
        deployment_missing.append("known_good_snapshot")
    if not verified_source:
        deployment_missing.append("verified_source")
    if not data.get("source_image_id"):
        deployment_missing.append("source_image")
    if not patch_ok:
        deployment_missing.append("patch_profile")
    if not root_ok:
        deployment_missing.append("deployment_root")
    if not launch_ok:
        deployment_missing.append("deployment_launch")
    if not recovery_ok:
        deployment_missing.append("recovery_checks")
    return {
        "monitoring": section(monitoring_missing),
        "restart": section(restart_missing),
        "configuration_recovery": section(recovery_missing),
        "code_fix": section(code_fix_missing),
        "deployment": section(deployment_missing),
    }
