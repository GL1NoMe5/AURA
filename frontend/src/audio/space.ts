// Original atmospheric cue: slow swells, open intervals and distant glass.
// Rendered ahead of time by tools/audio/render-space.mjs, never in the frame loop.
export const SPACE_RATE = 24000;
export const SPACE_SECONDS = 80;
export function renderSpace() {
  const length = SPACE_RATE * SPACE_SECONDS;
  const left = new Float32Array(length), right = new Float32Array(length);
  function swell(start: number, duration: number, frequency: number, level: number, pan: number) {
    const offset = Math.round(start * SPACE_RATE);
    for (let i = 0; i < duration * SPACE_RATE && offset + i < length; i++) {
      const t = i / SPACE_RATE, u = t / duration;
      const envelope = Math.sin(Math.PI * u) ** 2;
      const phase = 2 * Math.PI * frequency * t + .12 * Math.sin(t * .23);
      // Soft fundamental and restrained overtones, no sawtooth or beating unison.
      const value = level * envelope * (Math.sin(phase) + .09 * Math.sin(phase * 2) + .025 * Math.sin(phase * 3));
      const drift = pan + .13 * Math.sin(t * .17);
      left[offset + i] += value * Math.sqrt((1 - drift) / 2);
      right[offset + i] += value * Math.sqrt((1 + drift) / 2);
    }
  }
  // A spacious, unresolved progression; the lowest notes enter only in waves.
  [[0, 26, 73.416], [18, 30, 65.406], [40, 28, 82.407], [58, 22, 73.416]].forEach(([start, duration, root], index) => {
    swell(start, duration, root, .055, -.15);
    swell(start + 1, duration - 2, root * 2, .055, .25);
    swell(start + 3, duration - 4, root * 3, .037, -.4);
    swell(start + 5, duration - 6, root * (index % 2 ? 4.5 : 4), .025, .45);
  });
  [[2, 587.33], [8.7, 880], [15.9, 659.25], [25.4, 523.25], [31.6, 783.99],
    [39.2, 587.33], [47.5, 659.25], [54.7, 987.77], [62.9, 880], [70.8, 587.33]].forEach(([start, frequency], index) => {
    const offset = Math.round(start * SPACE_RATE), pan = Math.sin(index * 2.1) * .6;
    for (let i = 0; i < SPACE_RATE * 8 && offset + i < length; i++) {
      const t = i / SPACE_RATE, phase = 2 * Math.PI * frequency * t;
      const envelope = (1 - Math.exp(-t / .09)) * Math.min(1, (8 - t) / 2);
      const value = .12 * envelope * (Math.sin(phase) * Math.exp(-t / 2.3) + .12 * Math.sin(phase * 2.76) * Math.exp(-t / .6));
      left[offset + i] += value * Math.sqrt((1 - pan) / 2);
      right[offset + i] += value * Math.sqrt((1 + pan) / 2);
    }
  });
  // Diffuse reflections of the whole cue, baked once. No noise floor or feedback drone.
  const dryLeft = left.slice(), dryRight = right.slice();
  [[.37,.18],[.73,.14],[1.17,.11],[1.91,.08],[3.13,.05],[4.71,.03]].forEach(([delay, gain], tap) => {
    const offset = Math.round(delay * SPACE_RATE);
    for (let i = offset; i < length; i++) {
      left[i] += (tap % 2 ? dryRight : dryLeft)[i - offset] * gain;
      right[i] += (tap % 2 ? dryLeft : dryRight)[i - offset] * gain;
    }
  });
  // Both boundaries approach silence with zero slope.
  for (let i = 0; i < length; i++) {
    const edge = Math.min(1, i / (SPACE_RATE * 2), (length - 1 - i) / (SPACE_RATE * 5));
    const fade = Math.sin(edge * Math.PI / 2) ** 2;
    left[i] *= fade; right[i] *= fade;
  }
  return [left, right];
}
