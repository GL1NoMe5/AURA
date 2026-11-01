"""No-network checks of the live-evaluation call cap and workload accounting."""
import asyncio
from pathlib import Path
import sys

import httpx

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[1])]
from aura_ops.models import OpsError
from tools.ops.live_diagnosis import BudgetAdapter, load_requests


def test_single_call_baseline_never_receives_specialist_answers_or_expected_label():
    from tools.ops.compare_diagnoses import initial_packet
    packet = initial_packet({'observation.current': {'healthy': True},
        'source.index': {'files': ['app.py']}, 'specialist.findings': ['answer'],
        'observation.followup1': {'extra': 1}, 'source:app.py': 'selected-after-inference',
        'expected_action': 'abstain', 'diagnosis': {'action': 'abstain'}})
    assert set(packet) == {'observation.current', 'source.index'}


def test_parallel_roles_cannot_overrun_case_or_total_call_cap():
    class Delegate:
        ready = True
        calls = 0
        async def complete(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(0)
            return 'response'
    async def run():
        delegate = Delegate()
        adapter = BudgetAdapter(delegate, maximum=12)
        batch = await asyncio.gather(*(adapter.complete([]) for _ in range(11)), return_exceptions=True)
        assert batch.count('response') == 10
        assert isinstance(batch[-1], OpsError)
        assert delegate.calls == 10
        adapter.case_reserved = 0
        batch = await asyncio.gather(*(adapter.complete([]) for _ in range(4)), return_exceptions=True)
        assert batch.count('response') == 2
        assert delegate.calls == adapter.reserved == 12
    asyncio.run(run())


def test_workload_counts_failed_requests_and_stops_without_more_http(monkeypatch):
    async def run():
        stopped = asyncio.Event()
        record = {'requests': 0, 'successes': 0, 'failures': 0, 'elapsed_seconds': 0, 'max_latency_ms': 0}
        calls = []
        async def transport(request):
            calls.append(request)
            stopped.set()
            return httpx.Response(503)
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        monkeypatch.setattr('tools.ops.live_diagnosis.httpx.AsyncClient', lambda **kwargs: client)
        await load_requests('http://127.0.0.1:18093/health', stopped, record)
        assert record['requests'] == record['failures'] == len(calls) == 1
        assert record['successes'] == 0
    asyncio.run(run())
