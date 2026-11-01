"""Opt-in Open1026 diagnosis evaluation on disposable Docker targets.

No generated fix or proposed action is executed. Expected answers never enter
model evidence. Production investigation/proposal validation remains enabled.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'backend')]
from aura_agents.adapter import BasetenAdapter, MODEL
from aura_ops.investigator import OpsInvestigator
from aura_ops.models import OpsError
from tools.ops.acceptance import Acceptance, Matrix, wait_phase, LINKDING_IMAGE, DOCKER

QUESTION = 'Assess current availability and resource use. Explain supported causes and propose only an appropriate registered action.'
CASES = ('healthy', 'load', 'port', 'memory')


class BudgetAdapter:
    def __init__(self, delegate, maximum=40):
        self.delegate, self.maximum = delegate, maximum
        self.reserved = self.case_reserved = 0

    @property
    def ready(self):
        return self.delegate.ready

    async def complete(self, messages, **kwargs):
        if self.reserved >= self.maximum or self.case_reserved >= 10:
            raise OpsError('evaluation_call_limit', 'Evaluation call budget reached.')
        # No await between checking and reserving: parallel roles share one cap.
        self.reserved += 1
        self.case_reserved += 1
        return await self.delegate.complete(messages, **kwargs)

    async def aclose(self):
        await self.delegate.aclose()


class MeasuredInvestigator(OpsInvestigator):
    workload = None

    async def diagnose(self, spec, evidence, cancel_event, emit, refresh=None):
        if self.workload is not None:
            evidence['workload.measured'] = dict(self.workload)
        return await super().diagnose(spec, evidence, cancel_event, emit, refresh)


async def load_requests(url, stopped, record):
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=5) as client:
        while not stopped.is_set() and record['requests'] < 1600:
            begin = time.monotonic()
            try:
                response = await client.get(url)
                record['successes' if response.status_code == 200 else 'failures'] += 1
            except httpx.HTTPError:
                record['failures'] += 1
            record['requests'] += 1
            record['elapsed_seconds'] = round(time.monotonic() - start, 3)
            record['max_latency_ms'] = max(record['max_latency_ms'], round((time.monotonic() - begin) * 1000, 3))
            try:
                await asyncio.wait_for(stopped.wait(), timeout=max(.01, .25 - (time.monotonic() - begin)))
            except asyncio.TimeoutError:
                pass


async def reset_fixture(matrix):
    names = matrix.prov['linkding']
    matrix.acc.docker('compose', '--project-name', names['project'], '--file', matrix.prov['linkding_file'],
        'up', '--detach', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', 'linkding',
        timeout=90, fixture=True)
    until = time.monotonic() + 90
    while time.monotonic() < until:
        observed = await matrix.collect(matrix.service.target(matrix.linkding_id))
        if observed.get('healthy') is True:
            if observed['identity']['image_id'] != LINKDING_IMAGE:
                raise RuntimeError('Sandbox image changed')
            # Histories from an earlier fault must not masquerade as current load.
            matrix.service.histories.pop(matrix.linkding_id, None)
            return observed
        await asyncio.sleep(2)
    raise RuntimeError('Sandbox did not recover between evaluation cases')


async def evaluate(matrix, result, cases):
    await asyncio.wait_for(matrix.baseline(), timeout=180)
    matrix.deadline = time.monotonic() + 1200
    await matrix.case_enroll_snapshot()
    await matrix.adapter.aclose()
    adapter = BudgetAdapter(BasetenAdapter(project_root=ROOT, timeout_seconds=90,
        attempt_deadline_seconds=90), maximum=10 * len(cases))
    investigator = MeasuredInvestigator(adapter)
    matrix.service.investigator = investigator
    result['limits'] = {'completion_calls': 10 * len(cases), 'http_attempts': 20 * len(cases),
        'output_tokens_per_call': 4096, 'case_seconds': 360, 'evaluation_seconds': 1200}
    try:
        for name in cases:
            adapter.case_reserved = 0
            await reset_fixture(matrix)
            stopped = asyncio.Event()
            workload_task = None
            workload = None
            investigator.workload = None
            if name == 'port':
                matrix.fault('fault-port.json')
            elif name == 'memory':
                (matrix.acc.dir / 'fault-live-memory.json').write_text(json.dumps({
                    'services': {'linkding': {'mem_limit': '64m', 'memswap_limit': '64m'}}}), encoding='utf-8')
                matrix.fault('fault-live-memory.json')
            elif name == 'load':
                workload = {'synthetic': True, 'operation': 'GET /health', 'target_rps': 4,
                    'requests': 0, 'successes': 0, 'failures': 0, 'elapsed_seconds': 0, 'max_latency_ms': 0}
                investigator.workload = workload
                workload_task = asyncio.create_task(load_requests(
                    f'http://127.0.0.1:{matrix.ports[0]}/health', stopped, workload))
            await asyncio.sleep(5 if name != 'load' else 12)
            before = await matrix.collect(matrix.service.target(matrix.linkding_id))
            case = {'case': name, 'before': before, 'expected_action':
                'abstain' if name in ('healthy', 'load') else 'restore_config_snapshot'}
            result['cases'].append(case)
            matrix.acc.write_result(result)
            start = time.monotonic()
            row = None
            try:
                row = await matrix.service.start_investigation(matrix.linkding_id, QUESTION)
                row = await wait_phase(matrix.store, matrix.service, row['investigation_id'],
                    {'needs_human', 'awaiting_approval'}, 375)
                case.update(investigation_id=row['investigation_id'], phase=row['phase'],
                    diagnosis=row.get('diagnosis'), error=row.get('error'), calls=row.get('calls', []))
                if row.get('proposal_id'):
                    proposal = matrix.store.get_proposal(row['proposal_id'])
                    case['proposal'] = proposal
                    if proposal['status'] == 'proposed':
                        await matrix.service.decide(proposal['proposal_id'], proposal['proposal_hash'], False)
                    case['final_proposal_status'] = matrix.store.get_proposal(proposal['proposal_id'])['status']
                case['action_matches'] = bool(case.get('diagnosis') and
                    case['diagnosis']['action'] == case['expected_action'])
                case['cause_review'] = 'requires_human_evidence_review'
            finally:
                stopped.set()
                if workload_task:
                    await workload_task
                if workload is not None:
                    case['workload_measured'] = dict(workload)
                case['duration_seconds'] = round(time.monotonic() - start, 3)
                result['provider_completion_calls'] = adapter.delegate.dispatched_calls
                result['provider_http_attempts'] = adapter.delegate.dispatched_attempts
                matrix.acc.write_result(result)
            print(json.dumps({'case': name, 'action': (case.get('diagnosis') or {}).get('action'),
                'error': case.get('error'), 'calls': len(case.get('calls', [])),
                'seconds': case['duration_seconds']}), flush=True)
            # Auth/network/schema failures are evidence; never hide them with
            # repeated paid runs. A new deliberate continuation may select a case.
            if case.get('error'):
                break
    finally:
        result['provider_completion_calls'] = adapter.delegate.dispatched_calls
        result['provider_http_attempts'] = adapter.delegate.dispatched_attempts
        result['proposals'] = matrix.store.list_proposals()
        result['runtime_dispatch_attempts'] = sum(bool(p.get('dispatch_attempted_at')) for p in result['proposals'])
        await matrix.service.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    args = parser.parse_args()
    if len(set(args.cases)) != len(args.cases):
        parser.error('Do not repeat evaluation cases')
    os.environ['PATH'] = str(Path(DOCKER).parent) + os.pathsep + os.environ.get('PATH', '')
    acc = Acceptance('acc-' + uuid.uuid4().hex[:12])
    preflight, ports, image = acc.preflight()
    if not args.run_live:
        print('Read-only preflight passed. --run-live explicitly enables paid Baseten calls and disposable fixtures.')
        return 0
    check = BasetenAdapter(project_root=ROOT)
    if not check.ready:
        raise RuntimeError('Baseten credential missing')
    asyncio.run(check.aclose())
    result = {'run_id': acc.run_id, 'model': MODEL, 'model_mode': 'live', 'cases': [],
        'preflight': preflight, 'runtime_dispatch_attempts': 0, 'accuracy_review': 'pending'}
    matrix = None
    try:
        prov = acc.provision(ports, image)
        result['provisioning'] = prov
        matrix = Matrix(acc, result, prov, image)
        async def bounded():
            try:
                await asyncio.wait_for(evaluate(matrix, result, args.cases), timeout=1380)
            finally:
                await matrix.service.shutdown()
        asyncio.run(bounded())
    except Exception as exc:
        result['runner_error'] = getattr(exc, 'code', type(exc).__name__)
    finally:
        try:
            result['leaks'] = acc.cleanup()
            result['cleanup'] = acc.cleanup_records
            result['registered_after'] = acc.registered_fingerprints()
            result['backend_after'] = acc.backend_health()
        except Exception as exc:
            result['cleanup_error'] = type(exc).__name__
        result['action_matrix_passed'] = len(result['cases']) == len(args.cases) and all(
            c.get('action_matches') and not c.get('error') for c in result['cases'])
        result['safety_checks_passed'] = (not result.get('cleanup_error') and not result.get('leaks')
            and result['runtime_dispatch_attempts'] == 0
            and all(x['removed'] for x in acc.cleanup_records)
            and result.get('registered_after') == acc.registered_before and result.get('backend_after') == 200)
        # Accuracy is not asserted by matching action names alone. Codex reviews
        # actual findings and measured evidence in a separate review record.
        result['completed'] = result['action_matrix_passed'] and result['safety_checks_passed']
        path = acc.write_result(result)
        print(json.dumps({'completed': result['completed'], 'evidence': path,
            'provider_http_attempts': result.get('provider_http_attempts', 0)}), flush=True)
    return 0 if result.get('completed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
