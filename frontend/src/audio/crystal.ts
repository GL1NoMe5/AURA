// Original score for AURA: bowed glass strings and quiet, suspended harmonics.
// Generated once by tools/audio/render-crystal.mjs; never in the rendering loop.
export const CRYSTAL_RATE = 24000;
export const CRYSTAL_SECONDS = 96;

export function renderCrystal() {
  const length = CRYSTAL_RATE * CRYSTAL_SECONDS;
  const left = new Float32Array(length), right = new Float32Array(length);
  function bow(start: number, duration: number, frequency: number, strength: number, pan: number) {
    const offset = Math.round(start * CRYSTAL_RATE);
    let phase = 0;
    for (let i = 0; i < duration * CRYSTAL_RATE && offset + i < length; i++) {
      const t = i / CRYSTAL_RATE, progress = t / duration;
      // A string gently coming under tension: 18 cents below pitch, then settled.
      const tension = -18 * Math.exp(-t / 1.8) + .8 * Math.sin(t * 3.1);
      phase += 2 * Math.PI * frequency * 2 ** (tension / 1200) / CRYSTAL_RATE;
      const envelope = Math.sin(Math.PI * progress) ** 2;
      const shimmer = .085 * Math.sin(phase * 2.756) * Math.exp(-t / 3.2);
      const value = strength * envelope * (Math.sin(phase) + shimmer + .018 * Math.sin(phase * 4.07));
      const drift = pan + .1 * Math.sin(t * .34);
      left[offset + i] += value * Math.sqrt((1 - drift) / 2);
      right[offset + i] += value * Math.sqrt((1 + drift) / 2);
    }
  }
  function pluck(start: number, frequency: number, pan: number, strength: number) {
    const offset = Math.round(start * CRYSTAL_RATE), duration = 6;
    for (let i = 0; i < duration * CRYSTAL_RATE && offset + i < length; i++) {
      const t = i / CRYSTAL_RATE, phase = 2 * Math.PI * frequency * t;
      const envelope = (1 - Math.exp(-t / .04)) * Math.min(1, (duration - t) / 1.5);
      const value = strength * envelope * (Math.sin(phase) * Math.exp(-t / 1.6)
        + .11 * Math.sin(phase * 2.756) * Math.exp(-t / .38));
      left[offset + i] += value * Math.sqrt((1 - pan) / 2);
      right[offset + i] += value * Math.sqrt((1 + pan) / 2);
    }
  }
  // Open E-major/add-nine voicings. No bass oscillator or continuous noise floor.
  const phrases = [
    [0, 12, 415.305, .11, -.35], [4, 13, 739.989, .055, .3],
    [11, 15, 493.883, .09, .25], [20, 12, 622.254, .075, -.3],
    [28, 16, 369.994, .10, -.2], [34, 11, 554.365, .065, .4],
    [43, 14, 415.305, .10, .2], [51, 12, 659.255, .065, -.4],
    [60, 16, 493.883, .09, -.2], [66, 13, 739.989, .055, .35],
    [76, 14, 554.365, .075, .3], [83, 13, 415.305, .075, -.3],
  ];
  phrases.forEach(([start, duration, pitch, strength, pan]) => bow(start, duration, pitch, strength, pan));
  [[1.2, 987.767], [8.7, 739.989], [17.8, 1108.731], [26.4, 830.609],
    [38.1, 987.767], [47.5, 739.989], [57.2, 1244.508], [69.4, 987.767],
    [79.8, 830.609], [88.1, 739.989]].forEach(([start, pitch], index) =>
    pluck(start, pitch, Math.sin(index * 2.17) * .6, .075));

  // Diffuse, irregular stereo reflections: a large glass space without metallic feedback.
  const dryLeft = left.slice(), dryRight = right.slice();
  [[.173, .19], [.293, .16], [.487, .13], [.719, .105], [1.131, .085],
    [1.837, .065], [2.743, .045], [3.811, .025]].forEach(([delay, gain], tap) => {
    const shift = Math.round(delay * CRYSTAL_RATE);
    for (let i = shift; i < length; i++) {
      left[i] += (tap % 2 ? dryRight : dryLeft)[i - shift] * gain;
      right[i] += (tap % 2 ? dryLeft : dryRight)[i - shift] * gain;
    }
  });
  for (let i = 0; i < length; i++) {
    const edge = Math.min(1, i / (CRYSTAL_RATE * 1.5), (length - 1 - i) / (CRYSTAL_RATE * 3));
    const fade = Math.sin(edge * Math.PI / 2) ** 2;
    left[i] *= fade; right[i] *= fade;
  }
  return [left, right];
}

// A small resonant tap, rather than a notification chime. Cached by AudioContext.
export function renderGlassTap(rate: number) {
  const duration = .38, samples = new Float32Array(Math.ceil(rate * duration));
  for (let i = 0; i < samples.length; i++) {
    const t = i / rate, phase = 2 * Math.PI * 1046.502 * t;
    const attack = 1 - Math.exp(-t / .006);
    const fade = Math.min(1, (duration - t) / .07);
    samples[i] = .085 * attack * fade * (Math.sin(phase) * Math.exp(-t / .065)
      + .07 * Math.sin(phase * 2.756) * Math.exp(-t / .025));
  }
  return samples;
}
