import asyncio
import pytest
from aura_agents.adapter import _await_cancellable
from aura_agents.adapter import ProviderOutputError
from aura_agents.prompts import _load_json_object


@pytest.mark.parametrize('finish', [None, 'content_filter', 'tool_calls'])
def test_nonfinal_provider_output_is_rejected(finish):
    with pytest.raises(ProviderOutputError):
        _load_json_object('{}', finish)


def test_outer_cancellation_awaits_inner_http_task():
    async def run():
        entered, closed = asyncio.Event(), asyncio.Event()
        async def request():
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                closed.set()
        outer = asyncio.create_task(_await_cancellable(request(), asyncio.Event()))
        await entered.wait()
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert closed.is_set()
    asyncio.run(run())
