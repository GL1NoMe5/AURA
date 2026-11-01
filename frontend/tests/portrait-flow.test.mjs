import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PortraitFlow } from '../src/scene/portraitFlow.ts';

test('stationary pointer does not excavate portrait; strokes carry it and settle', () => {
  const flow = new PortraitFlow(new Float32Array([0, 0, 0, 2, 2, 0]));
  flow.pointer(-0.12, 0);
  for (let i = 0; i < 120; i++) flow.update(1 / 120, true);
  assert.equal(flow.offsets[0], 0);
  flow.pointer(0.12, 0);
  flow.update(1 / 120, true);
  assert.ok(flow.offsets[0] > 0 && flow.offsets[0] < 0.003, 'first movement is continuous');
  for (let i = 0; i < 24; i++) flow.update(1 / 120, true);
  assert.ok(flow.offsets[0] > 0.005, 'push follows stroke direction');
  assert.equal(flow.offsets[2], 0, 'distant portrait stays intact');
  flow.leave();
  for (let i = 0; i < 480; i++) flow.update(1 / 120, true);
  assert.ok(Math.abs(flow.offsets[0]) < 0.0001);
});

test('integration remains consistent at 20, 60 and 240 FPS', () => {
  const results = [20, 60, 240].map(fps => {
    const flow = new PortraitFlow(new Float32Array([0, 0.03, 0]));
    flow.pointer(-0.12, 0); flow.pointer(0.12, 0);
    for (let i = 0; i < fps / 2; i++) flow.update(1 / fps, true);
    return flow.offsets[0];
  });
  assert.ok(Math.max(...results) - Math.min(...results) < 0.002);
});

test('reducing density clears hidden momentum before those particles return', () => {
  const flow = new PortraitFlow(new Float32Array([0, 0, 0, 0, 0.01, 0]));
  flow.pointer(-0.12, 0); flow.pointer(0.12, 0);
  for (let i = 0; i < 12; i++) flow.update(1 / 120, true, 2);
  assert.ok(flow.offsets[2] > 0);
  flow.update(1 / 120, false, 1);
  assert.equal(flow.offsets[2], 0);
  assert.equal(flow.offsets[3], 0);
  flow.update(1 / 120, false, 2);
  assert.equal(flow.offsets[2], 0, 'hidden particles return at their source position');
});

test('hover can push grains during the return immediately after releasing attraction', () => {
  const positions = new Float32Array([0.05, 0.02, 0, 2, 2, 0]);
  const hovered = new PortraitFlow(positions), reference = new PortraitFlow(positions);
  for (const flow of [hovered, reference]) {
    flow.attract(0, 0);
    for (let i = 0; i < 60; i++) flow.update(1 / 120, true);
    flow.release(0.5); flow.leave();
    for (let i = 0; i < 12; i++) flow.update(1 / 120, true);
  }
  const x = positions[0] * 1.38 + hovered.offsets[0];
  const y = positions[1] * 1.38 + hovered.offsets[1];
  hovered.pointer(x - 0.10, y);
  hovered.pointer(x + 0.10, y);
  for (let i = 0; i < 12; i++) { hovered.update(1 / 120, true); reference.update(1 / 120, true); }
  assert.ok(Math.hypot(hovered.offsets[0] - reference.offsets[0], hovered.offsets[1] - reference.offsets[1]) > 0.001,
    'release/return must not suppress the ordinary sand brush');
});
