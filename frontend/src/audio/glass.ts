// Sparse glass phrases. All voices decay to silence; there is no drone or noise bed.
export const GLASS_RATE = 24000;
export const GLASS_SECONDS = 64;
export function renderGlass() {
  const channels = [new Float32Array(GLASS_RATE * GLASS_SECONDS), new Float32Array(GLASS_RATE * GLASS_SECONDS)];
  const notes = [587.33, 659.25, 783.99, 880, 1174.66];
  const score = [[.2,0],[2.4,2],[4.1,3],[7.4,1],[9.2,0],[12.8,4],[14.1,2],[17.7,1],
    [20.6,3],[22.1,2],[25.9,0],[29.5,1],[31.2,4],[33.8,3],[37.9,2],[39.3,0],
    [43.1,1],[46.5,3],[48.2,4],[51.7,2],[54.9,1],[57.1,0]];
  score.forEach(([start, note], index) => {
    const frequency = notes[note], offset = Math.round(start * GLASS_RATE);
    const pan = Math.sin(index * 2.4) * .55;
    const left = Math.sqrt((1 - pan) / 2), right = Math.sqrt((1 + pan) / 2);
    const velocity = .19 + (index % 4) * .018;
    for (let i = 0; i < GLASS_RATE * 6; i++) {
      const t = i / GLASS_RATE, phase = 2 * Math.PI * frequency * t;
      const attack = 1 - Math.exp(-t / .018);
      const tail = Math.min(1, (6 - t) * 2);
      const value = velocity * attack * tail * (
        .82 * Math.sin(phase) * Math.exp(-t / 1.25) +
        .14 * Math.sin(phase * 2.76) * Math.exp(-t / .42) +
        .04 * Math.sin(phase * 4.07) * Math.exp(-t / .18));
      channels[0][offset + i] += value * left;
      channels[1][offset + i] += value * right;
    }
  });
  return channels;
}
