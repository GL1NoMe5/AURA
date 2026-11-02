import { test } from 'node:test';
import assert from 'node:assert/strict';
import { visibleRun } from '../src/demo/presentation.ts';

test('fresh page keeps completed success, failure and cancellation behind the result button', () => {
  for (const phase of ['recovered', 'needs_help', 'cancelled', 'interrupted']) {
    const run = { run_id: 'old', finished_at: 1, phase };
    assert.equal(visibleRun(run, null), null);
    assert.equal(visibleRun(run, 'old'), run);
    assert.equal(visibleRun(run, 'different'), null);
  }
  assert.equal(visibleRun(null, null), null);
});

test('reconnection follows a running investigation and keeps its final result', () => {
  const active = { run_id: 'new', phase: 'investigating' };
  assert.equal(visibleRun(active, null), active);
  const finished = { ...active, phase: 'needs_help', finished_at: 1 };
  assert.equal(visibleRun(finished, active.run_id), finished);
  assert.equal(visibleRun(finished, null), null);
});
