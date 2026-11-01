export const MIN_PARTICLES = 10_000;
export const MAX_PARTICLES = 120_000;
export const DEFAULT_PARTICLES = 60_000;
export const PARTICLE_STEP = 1_000;
export function normalizeParticleCount(value: number) {
  return Number.isFinite(value)
    ? Math.max(MIN_PARTICLES, Math.min(MAX_PARTICLES, Math.round(value / PARTICLE_STEP) * PARTICLE_STEP))
    : DEFAULT_PARTICLES;
}
export const formatParticles = (value: number) => Math.round(value).toLocaleString('en-US');
