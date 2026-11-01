import importlib.util
from pathlib import Path


def test_live_harness_failure_is_nonzero(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[2] / 'tools/preflight/live_run.py'
    spec = importlib.util.spec_from_file_location('review_live_run', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_run_live', lambda *args: {
        'mode': 'live', 'incident_started': True,
        'final_state': {'phase': 'error'}, 'child_alive_after_shutdown': False,
    })
    assert module.main(['--live', '--output', str(tmp_path/'failed.json')]) == 1


def test_repeat_run_requires_new_rejections_not_prior_counter():
    import asyncio
    from test_api import FakeController
    from aura_api.manager import IncidentManager, RunContext
    from aura_api.config import ApiSettings
    async def run():
        worker = FakeController()
        worker.failed = 50
        worker.retained = 96 * 1024 * 1024
        manager = IncidentManager(controller=worker, settings=ApiSettings(failure_evidence_timeout_seconds=.02))
        context = RunContext('repeat', 'cache_retention', worker_generation=worker.generation, failed_jobs_at_start=50)
        manager._current_run = context
        assert not await manager._wait_for_failure_evidence(context)
        worker.failed = 51
        assert await manager._wait_for_failure_evidence(context)
    asyncio.run(run())
