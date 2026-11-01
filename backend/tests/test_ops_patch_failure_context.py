"""Failure-focused context is bounded, source-backed and never executes tests."""
import asyncio
from test_ops_patch_retry_contract import RawAdapter, EDITS_ONLY, _packet
from aura_ops.patches import _failed_test_context
from test_ops_patches import Runner, candidate, run_engine


def phases(node):
    return {(node, 'call'): {'outcome': 'failed'}}


def test_selects_exact_class_method_and_ignores_parameter_values():
    source = b'class Checks:\n    def test_missing(self):\n        assert 404 == 404\n'
    result = _failed_test_context(phases('tests/check.py::Checks::test_missing[private-value]'),
                                  {'tests/check.py': source})
    assert len(result) == 1
    assert result[0]['content'] == source.decode().rstrip()
    assert 'private-value' not in str(result)
    assert result[0]['start_line'] == 1


def test_unknown_paths_syntax_and_symbols_do_not_read_disk(tmp_path):
    secret = tmp_path / 'outside.py'
    secret.write_text('never include this')
    assert _failed_test_context(phases(str(secret) + '::test_x'), {}) == []
    assert _failed_test_context(phases('x.py::test_x'), {'x.py': b'not valid python !'}) == []
    assert _failed_test_context(phases('x.py::test_x'), {'x.py': b'def other(): pass'}) == []


def test_duplicate_failures_and_size_limits():
    entries = phases('x.py::test_x')
    entries[('x.py::test_x', 'teardown')] = {'outcome': 'failed'}
    assert len(_failed_test_context(entries, {'x.py': b'def test_x(): pass'})) == 1
    huge = ('def test_x():\n    x = ' + repr('a' * 6500)).encode()
    assert _failed_test_context(entries, {'x.py': huge}) == []


def test_original_failure_context_reaches_retry_without_changing_tests(tmp_path):
    async def run():
        adapter = RawAdapter([candidate(), dict(EDITS_ONLY)])
        runner = Runner({('original-suite', 1): {'mode': 'single-fail'}})
        result, source, _ = await run_engine(tmp_path, adapter=adapter, runner=runner)
        assert result['status'] == 'tests_passed'
        packet = _packet(adapter, 1)
        assert packet['failed_test_context'][0]['path'] == 'tests/test_app.py'
        assert 'assert True' in packet['failed_test_context'][0]['content']
        assert sum(len(x['content']) for group in ('reference_tests', 'failed_test_context')
                   for x in packet[group]) <= 24000
        assert (source / 'tests/test_app.py').read_text() == 'def test_existing():\n    assert True\n'
        assert len(adapter.calls) == 2
    asyncio.run(run())
