import { test } from 'node:test';
import assert from 'node:assert/strict';
import { emptyRuntime, applySnapshot, applyEvent, sceneForState } from '../src/runtime/model.ts';

const snapshot = (extra = {}) => ({ seq: 10, server_instance_id: 'one', run_id: 'run1', phase: 'diagnosing', telemetry: { rss_bytes: 50, rss_fresh: true, retained_cache_bytes: 20, status_fresh: true, sample_timestamp: '2026-10-27T10:00:00Z' }, agents: [{ agent_id: 'logs', status: 'running' }], latest_verification: null, simulated_diagnosis: false, ...extra });
const event = (seq, type, payload = {}, extra = {}) => ({ seq, server_instance_id: 'one', run_id: 'run1', type, payload, timestamp: '2026-10-27T10:00:01Z', ...extra });

test('recovery portrait requires verification, idle portrait is ambient', () => {
  assert.equal(sceneForState(null), 'portrait');
  assert.equal(sceneForState(snapshot({ phase: 'idle' })), 'portrait');
  assert.equal(sceneForState(snapshot({ phase: 'resolved' })), 'streams');
  assert.equal(sceneForState(snapshot({ phase: 'error', latest_verification: { verified: true } })), 'streams');
  assert.equal(sceneForState(snapshot({ phase: 'resolved', latest_verification: { verified: true } })), 'portrait');
  assert.equal(sceneForState(snapshot({ phase: 'resolved', simulated_diagnosis: true, latest_verification: { verified: true } })), 'streams');
});
test('reset snapshot drops old verification and late messages cannot resolve idle', () => {
  let r = applySnapshot(emptyRuntime(), snapshot({ phase: 'resolved', latest_verification: { verified: true } }));
  r = applySnapshot(r, snapshot({ seq: 20, run_id: null, phase: 'idle', latest_verification: null }));
  r = applyEvent(r, event(21, 'phase_changed', { to: 'resolved' }));
  assert.equal(r.state.phase, 'idle'); assert.equal(sceneForState(r.state), 'portrait');
});
test('old sequence and foreign server events are ignored; server restart accepts new snapshot', () => {
  let r = applySnapshot(emptyRuntime(), snapshot());
  r = applyEvent(r, event(9, 'phase_changed', { to: 'resolved' }));
  r = applyEvent(r, event(11, 'phase_changed', { to: 'resolved' }, { server_instance_id: 'two' }));
  assert.equal(r.state.phase, 'diagnosing');
  r = applySnapshot(r, snapshot({ seq: 1, server_instance_id: 'two', phase: 'idle', run_id: null }));
  assert.equal(r.server, 'two'); assert.equal(r.seq, 1);
});
test('new run clears former success; resync discards buffered events preceding watermark', () => {
  let r = applySnapshot(emptyRuntime(), snapshot({ phase: 'resolved', latest_verification: { verified: true } }));
  r = applyEvent(r, event(11, 'phase_changed', { to: 'fault_injected' }, { run_id: 'run2' }));
  assert.equal(r.state.latest_verification, null); assert.equal(sceneForState(r.state), 'streams');
  r = applySnapshot(r, snapshot({ seq: 15, run_id: 'run2', phase: 'verifying' }));
  r = applyEvent(r, event(14, 'phase_changed', { to: 'diagnosing' }, { run_id: 'run2' }));
  assert.equal(r.state.phase, 'verifying');
});
test('unknown/stale metrics stay null and retained samples/logs are bounded', () => {
  let r = applySnapshot(emptyRuntime(), snapshot());
  for (let n = 11; n < 250; n++) {
    r = applyEvent(r, event(n, 'telemetry', { rss_bytes: 42, retained_cache_bytes: 42, rss_fresh: false, status_fresh: false, sample_timestamp: String(n) }));
  }
  assert.equal(r.samples.length, 120); assert.equal(r.samples.at(-1).rss, null); assert.equal(r.samples.at(-1).cache, null);
  for (let n = 250; n < 400; n++) r = applyEvent(r, event(n, 'agent_message', { agent_id: 'logs', kind: 'finding', message: 'x' }));
  assert.equal(r.log.length, 100);
});
