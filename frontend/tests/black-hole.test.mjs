import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PortraitFlow } from '../src/scene/portraitFlow.ts';

function cloud() {
  const positions = new Float32Array(160 * 3);
  for (let i = 0; i < 160; i++) {
    positions[i * 3] = Math.cos(i * 2.39996) * (0.5 + i / 80);
    positions[i * 3 + 1] = Math.sin(i * 2.39996) * (0.5 + i / 80);
  }
  return { positions, flow: new PortraitFlow(positions) };
}
function advance(flow, seconds, fps = 60) {
  for (let i = 0; i < Math.round(seconds * fps); i++) flow.update(1 / fps, true);
}
function averageRadius({ positions, flow }) {
  let sum = 0;
  for (let i = 0, p = 0; p < positions.length; i += 2, p += 3)
    sum += Math.hypot(positions[p] * 1.38 + flow.offsets[i] - flow.holeX,
      (positions[p + 1] * 1.38 + flow.offsets[i + 1] - flow.holeY) / 0.4);
  return sum / (positions.length / 3);
}

test('holding draws the whole cloud into a bounded moving orbit', () => {
  const state = cloud(), { flow } = state;
  const initial = averageRadius(state);
  flow.attract(0.2, -0.1);
  advance(flow, 7);
  assert.ok(averageRadius(state) < initial * 0.35);
  assert.ok(averageRadius(state) > 0.3, 'orbit retains a dark centre');
  const before = flow.offsets.slice();
  advance(flow, 0.2);
  assert.ok(before.some((v, i) => Math.abs(v - flow.offsets[i]) > 0.02), 'grains keep orbiting');
  flow.pointer(-0.6, 0.4); advance(flow, 2);
  assert.ok(Math.abs(flow.holeX + 0.6) < 0.001);
  assert.ok(averageRadius(state) < 1.3);
});

test('longer holds eject faster, remain finite, then smoothly restore the portrait', () => {
  function ejected(seconds) {
    const { flow } = cloud(); flow.attract(0, 0); advance(flow, 2);
    flow.release(seconds);
    const before = flow.offsets.slice();
    flow.update(1 / 240, true);
    const motion = flow.offsets.reduce((sum, v, i) => sum + Math.abs(v - before[i]), 0);
    return { flow, motion };
  }
  const short = ejected(0.2), long = ejected(8), extreme = ejected(100000);
  assert.ok(long.motion > short.motion * 1.5);
  assert.ok(extreme.motion < long.motion * 1.08, 'charge has a finite ceiling');
  advance(long.flow, 0.8);
  assert.ok(Math.max(...long.flow.offsets.map(Math.abs)) > 3, 'burst reaches across the viewport');
  advance(long.flow, 2.2);
  assert.ok(Math.max(...long.flow.offsets.map(Math.abs)) < 0.04, 'portrait is substantially restored within three seconds');
  advance(long.flow, 6.8);
  assert.ok(Math.max(...long.flow.offsets.map(Math.abs)) < 0.002, 'all grains return home');
  assert.ok(long.flow.offsets.every(Number.isFinite));
});

test('cancel, reset and disabled interaction cannot retain a captured cloud', () => {
  const { flow } = cloud(); flow.attract(0, 0); advance(flow, 2);
  flow.cancelAttraction(); advance(flow, 5);
  assert.equal(flow.holding, false);
  assert.ok(Math.max(...flow.offsets.map(Math.abs)) < 0.002);
  flow.attract(0.5, 0); advance(flow, 1); flow.release(); advance(flow, 0.3);
  flow.attract(-0.5, 0); advance(flow, 1); flow.reset();
  assert.equal(flow.holding, false);
  assert.equal(flow.holeGlow, 0);
  assert.ok(flow.offsets.every(v => v === 0));
  flow.attract(0, 0); flow.update(1 / 60, false);
  assert.equal(flow.holding, false);
});

test('attraction and burst remain consistent across 20, 60 and 240 FPS', () => {
  const results = [20, 60, 240].map(fps => {
    const { flow } = cloud(); flow.attract(0.2, 0); advance(flow, 3, fps);
    flow.release(3); advance(flow, 1, fps); return flow.offsets;
  });
  for (const value of results.slice(1))
    assert.ok(value.every((v, i) => Math.abs(v - results[0][i]) < 0.005));
});
