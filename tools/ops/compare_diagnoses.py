"""Four opt-in single-call baselines on retained initial live evidence.

No Docker access, actions or fixture mutations. Expected outcomes and specialist
answers are excluded from model input. Records measured usage, not billing.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from aura_agents.adapter import BasetenAdapter, MODEL
from aura_ops.investigator import _SYSTEM
from aura_ops.models import Diagnosis
from aura_ops.redaction import redact_data
from aura_ops.store import OpsStore

SOURCES = {'healthy': 'acc-b053bf6ea3f4', 'load': 'acc-b053bf6ea3f4',
           'port': 'acc-dde9f156adb2', 'memory': 'acc-dde9f156adb2'}


def initial_packet(evidence):
    # Only evidence available before specialist inference. Do not leak their
    # conclusions, follow-up measurements, fetched source or a previous answer.
    exact = {'observation.current', 'observation.history', 'operator.question',
             'target.policy', 'target.baseline', 'history.confirmed',
             'workload.measured', 'source.index', 'source.unavailable'}
    return redact_data({k: v for k, v in evidence.items() if k in exact})


async def run():
    adapter = BasetenAdapter(project_root=ROOT, timeout_seconds=90, attempt_deadline_seconds=90)
    report = {'model': MODEL, 'mode': 'single_call', 'cases': [],
              'actions_executed': 0, 'accuracy_review': 'pending',
              'limitations': ['One observation per case, not a statistical benchmark.',
                'Healthy/load swarm used the earlier authorization prompt; configuration cases use the updated prompt.',
                'Single call has initial evidence but no follow-up source/measurement tool rounds.']}
    output = ROOT / '.local/ops/evidence/single-call-comparison.json'
    try:
        for name, run_id in SOURCES.items():
            folder = ROOT / '.local/ops-acceptance' / run_id
            source = json.loads((folder / 'result.json').read_text())
            measured = next(c for c in source['cases'] if c['case'] == name)
            store = OpsStore(folder / 'store')
            try:
                investigation = store.get_investigation(measured['investigation_id'])
            finally:
                store.close()
            packet = initial_packet(investigation['evidence'])
            schema = Diagnosis.model_json_schema()
            schema['$defs']['Finding']['properties']['evidence_refs']['items']['enum'] = sorted(packet)
            instructions = ('Diagnose in one pass using only this measured evidence. '
                'Return the JSON Diagnosis schema. No markdown. If healthy or evidence is insufficient, abstain. '
                'Never propose deploy_verified_patch during diagnosis; restart is temporary mitigation. '
                'Recommend only an evidence-supported registered action, subject to backend validation and approval.\n')
            record = {'case': name, 'source_run': run_id, 'investigation_id': measured['investigation_id'],
                      'swarm_action': measured['diagnosis']['action'],
                      'swarm_seconds': measured['duration_seconds'], 'swarm_calls': len(measured['calls'])}
            report['cases'].append(record)
            start = time.monotonic()
            try:
                result = await adapter.complete([
                    {'role': 'system', 'content': _SYSTEM + '\n' + instructions + json.dumps(schema)},
                    {'role': 'user', 'content': 'EVIDENCE DATA:\n' + json.dumps(packet)}], max_tokens=4096)
                record['usage'] = result.usage.model_dump() if result.usage else None
                record['attempts'] = result.attempts
                record['provider_duration_ms'] = result.duration_ms
                if result.finish_reason != 'stop':
                    raise ValueError('incomplete_response')
                diagnosis = Diagnosis.model_validate_json(result.content, strict=True)
                if diagnosis.action.value == 'deploy_verified_patch' or any(
                        not set(f.evidence_refs).issubset(packet) for f in diagnosis.findings):
                    raise ValueError('invalid_action_or_evidence')
                record['diagnosis'] = diagnosis.model_dump(mode='json')
            except Exception as exc:
                record['error'] = getattr(exc, 'category', type(exc).__name__)
            record['seconds'] = round(time.monotonic() - start, 3)
            report['http_attempts'] = adapter.dispatched_attempts
            output.write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps({'case': name, 'action': (record.get('diagnosis') or {}).get('action'),
                'seconds': record['seconds'], 'error': record.get('error')}), flush=True)
    finally:
        await adapter.aclose()
        report['http_attempts'] = adapter.dispatched_attempts
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    args = parser.parse_args()
    if args.run_live:
        asyncio.run(run())
    else:
        print('Offline default. --run-live enables at most four Baseten completions (eight HTTP attempts with bounded retries).')
