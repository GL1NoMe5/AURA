import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_api import FakeController
from aura_api.manager import IncidentManager, RunContext
from aura_api.config import ApiSettings
from aura_api.schemas import Phase
from aura_worker.controller import ControllerState


def test_worker_death_cancels_hung_diagnosis_promptly():
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class Hung:
            ready = True
            simulated = True
            async def diagnose(self, evidence, cancel_event, sink):
                entered.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()
        worker = FakeController()
        manager = IncidentManager(controller=worker, orchestrator=Hung(), settings=ApiSettings(orchestration_timeout_seconds=30))
        context = RunContext('death-review', 'cache_retention', worker_generation=1, agents=manager._new_run_agents())
        manager._current_run = context
        task = asyncio.create_task(manager._diagnose_and_remediate(context))
        await entered.wait()
        worker.is_running = False
        worker.state = ControllerState.DEAD
        await asyncio.wait_for(task, 1)
        assert context.phase == Phase.ERROR
        assert context.error.code == 'worker_dead'
        assert cancelled.is_set()
        assert worker.restore_calls == 0
        await manager.reset()
        assert manager.snapshot().phase == Phase.IDLE
        await manager.shutdown()
    asyncio.run(run())
