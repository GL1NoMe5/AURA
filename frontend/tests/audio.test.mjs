import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { CRYSTAL_RATE, CRYSTAL_SECONDS, renderGlassTap } from '../src/audio/crystal.ts';

test('shipped crystal cue is complete stereo PCM with quiet loop boundaries and headroom', () => {
  const wav = fs.readFileSync(new URL('../public/audio/aura-crystal.wav', import.meta.url));
  assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
  assert.equal(wav.readUInt16LE(22), 2);
  assert.equal(wav.readUInt32LE(24), CRYSTAL_RATE);
  assert.equal(wav.readUInt16LE(34), 16);
  assert.equal(wav.length, 44 + CRYSTAL_RATE * CRYSTAL_SECONDS * 4);
  let peak = 0, energy = 0, difference = 0;
  for (let i = 44; i < wav.length; i += 4) {
    const left = wav.readInt16LE(i) / 32768, right = wav.readInt16LE(i + 2) / 32768;
    peak = Math.max(peak, Math.abs(left), Math.abs(right));
    energy += left * left + right * right; difference += Math.abs(left - right);
  }
  assert.ok(peak > .08 && peak < .3, `peak ${peak}`);
  assert.ok(Math.sqrt(energy / ((wav.length - 44) / 2)) > .015, 'cue must contain actual audio');
  assert.ok(difference > 100, 'stereo field must not collapse to identical mono');
  for (let i = 0; i < 40; i += 2) {
    assert.equal(wav.readInt16LE(44 + i), 0);
    assert.equal(wav.readInt16LE(wav.length - 2 - i), 0);
  }
});

test('button tap has a bounded duration, soft onset and a complete decay at common device rates', () => {
  for (const rate of [24000, 44100, 48000, 96000]) {
    const tap = renderGlassTap(rate);
    assert.equal(tap.length, Math.ceil(rate * .38));
    assert.equal(tap[0], 0);
    let peak = 0;
    for (const sample of tap) { assert.ok(Number.isFinite(sample)); peak = Math.max(peak, Math.abs(sample)); }
    assert.ok(peak > .03 && peak < .09, `tap headroom at ${rate}`);
    assert.ok(Math.abs(tap[tap.length - 1]) < .000001, `tap must end softly at ${rate}`);
  }
});
