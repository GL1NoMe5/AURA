import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PortraitFlow } from '../src/scene/portraitFlow.ts';

test('settled portrait reports no motion and idle updates keep it exactly at rest', () => {
  const flow = new PortraitFlow(new Float32Array([0, 0, 0, 0.4, -0.2, 0]));
  assert.equal(flow.hasMotion, false);
  flow.pointer(0.1, 0.1);
  for (let i = 0; i < 30; i++) flow.update(1 / 60, true);
  assert.equal(flow.hasMotion, false, 'a converged cursor over a settled portrait is not motion');
  assert.ok(flow.offsets.every(v => v === 0));
});

test('hold reports motion until the cloud settles again after cancellation', () => {
  const positions = new Float32Array(90 * 3);
  for (let i = 0; i < 90; i++) {
    positions[i * 3] = Math.cos(i * 2.39996) * (0.5 + i / 45);
    positions[i * 3 + 1] = Math.sin(i * 2.39996) * (0.5 + i / 45);
  }
  const flow = new PortraitFlow(positions);
  flow.attract(0, 0);
  for (let i = 0; i < 60; i++) flow.update(1 / 60, true);
  assert.equal(flow.hasMotion, true);
  flow.cancelAttraction();
  for (let i = 0; i < 60 * 6; i++) flow.update(1 / 60, true);
  assert.ok(Math.max(...flow.offsets.map(Math.abs)) < 0.002);
  let settled = false;
  for (let i = 0; i < 60 * 20 && !settled; i++) { flow.update(1 / 60, true); settled = !flow.hasMotion; }
  assert.ok(settled, 'motion flag clears once every grain is back at rest');
});

test('refreshPositions picks up positions filled after construction', () => {
  const data = [0.5, 0.25, 0, -0.3, 0.8, 0];
  const direct = new PortraitFlow(new Float32Array(data));
  const positions = new Float32Array(data.length);
  const refreshed = new PortraitFlow(positions);
  positions.set(data);
  refreshed.refreshPositions();
  for (const flow of [direct, refreshed]) {
    flow.attract(0.1, 0.1);
    for (let i = 0; i < 90; i++) flow.update(1 / 60, true);
    flow.release(2);
    for (let i = 0; i < 90; i++) flow.update(1 / 60, true);
  }
  assert.ok(direct.offsets.every((v, i) => v === refreshed.offsets[i]),
    'late-filled positions produce the identical trajectory');
});
