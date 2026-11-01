import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RenderCheck } from '../src/scene/renderCheck.ts';
const stats = { seconds: 1, frames: 60, fps: 60, cpuMs: 1, geometries: 4, textures: 13, heapBytes: null, width: 1920, height: 1080, portraitReady: true, gpu: 'test', points: 60000 };
const settings = { paused: false, reducedMotion: false, quality: 'full' };
test('a short trial cannot claim final acceptance', () => {
  const check = new RenderCheck(10);
  for (let i = 0; i < 15; i++) check.add(stats, settings, 'portrait');
  assert.equal(check.report.acceptance_pass, false);
  assert.equal(check.report.average_fps, 60);
});
test('full check requires every mode, stable resources and no interruptions', () => {
  const check = new RenderCheck(1200);
  for (let i = 0; i < 1205; i++) check.add(stats, settings, check.mode);
  assert.equal(check.report.acceptance_pass, true);
  const bad = new RenderCheck(1200);
  bad.issues.add('Tab hidden during check');
  for (let i = 0; i < 1205; i++) bad.add(stats, settings, bad.mode);
  assert.equal(bad.report.acceptance_pass, false);
});
