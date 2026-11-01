"""Run enrolled trusted tests with AURA's real patch sandbox, without a model.

Uses the freshly exported linkding lab target and immutable test image. Writes
only an independent local evidence store/workspace; never patches the target.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from aura_ops.models import OpsError, TargetSpec
from aura_ops.patches import PatchEngine, _source_files, _write_workspace
from aura_ops.source import SourceIndex
from aura_ops.store import OpsStore
from aura_ops.test_protocol import check_collect, check_suite


async def check():
    local_docker = ROOT / ".local/environment/Docker/resources/bin"
    if (local_docker / "docker.exe").is_file():
        os.environ["PATH"] = str(local_docker) + os.pathsep + os.environ.get("PATH", "")
    spec = TargetSpec.model_validate_json((ROOT / ".local/ops-lab/target.json").read_text("utf-8"))
    run_id = "upstream-" + uuid.uuid4().hex
    folder = ROOT / ".local/ops-lab/evidence" / run_id
    store = OpsStore(folder)
    try:
        engine = PatchEngine(store, None, folder)
        await engine._image_available(spec)
        _, files = _source_files(spec, SourceIndex(spec), engine._secrets())
        workspace = folder / "workspace"
        hashes = _write_workspace(workspace, files)
        collected, collection_record, baseline = await engine._test(
            spec, workspace, hashes, spec.patch_profile.test_paths,
            asyncio.Event(), run_id, "1-collect")
        if not check_collect(baseline["collected"], baseline["phases"],
                             exit_code=collected.returncode):
            raise OpsError("test_collection_failed", "Trusted baseline collection is incomplete.", 409)
        result, record, outcome = await engine._test(
            spec, workspace, hashes, spec.patch_profile.test_paths,
            asyncio.Event(), run_id, "1-original-suite")
        record["passed"] = check_suite(outcome["collected"], outcome["phases"],
                                       exit_code=result.returncode,
                                       baseline_nodes=baseline["collected"])
        record["baseline_collection"] = collection_record
        record.update(provider_requests=0, source_revision=spec.source_revision,
                      test_image=spec.patch_profile.image, scratch_dirs=spec.patch_profile.scratch_dirs)
        path = folder / "result.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps({"passed": record["passed"], "evidence": str(path), "provider_requests": 0}))
        return 0 if record["passed"] else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
