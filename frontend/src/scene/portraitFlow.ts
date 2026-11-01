// Persistent momentum, rather than timed radial holes. The brush's compact
// stream-function kernel carries points forward and curls around either side.
//
// Performance notes (PERF-001): the integrator keeps the original fixed
// 240 Hz substep grid, which is what makes 20/60/240 FPS trajectories agree.
// Speed comes from precomputed per-grain constants, avoiding Math.hypot and
// per-substep hash recomputation, and skipping particles that are exactly at
// rest with no force acting on them. Settled flow also reports hasMotion so
// the renderer can skip redundant buffer uploads.
export class PortraitFlow {
  readonly offsets: Float32Array;
  private readonly velocities: Float32Array;
  private readonly grains: Float32Array;
  private readonly baseX: Float32Array;
  private readonly baseY: Float32Array;
  private readonly orbitRadius: Float32Array;
  private readonly orbitSpin: Float32Array;
  private readonly burstCos: Float32Array;
  private readonly burstSin: Float32Array;
  private readonly burstScale: Float32Array;
  private x = 0;
  private y = 0;
  private targetX = 0;
  private targetY = 0;
  private active = false;
  holding = false;
  holdSeconds = 0;
  holeGlow = 0;
  burstGlow = 0;
  holeX = 0;
  holeY = 0;
  hasMotion = false;
  private settled = true;
  private burstAge = 100;
  private burstSpeed = 0;
  private viewportAspect = 1.7;
  private previousCount = 0;
  private readonly positions: Float32Array;
  constructor(positions: Float32Array) {
    this.positions = positions;
    const count = positions.length / 3;
    this.offsets = new Float32Array(count * 2);
    this.velocities = new Float32Array(this.offsets.length);
    this.grains = new Float32Array(this.offsets.length);
    this.baseX = new Float32Array(count);
    this.baseY = new Float32Array(count);
    this.orbitRadius = new Float32Array(count);
    this.orbitSpin = new Float32Array(count);
    this.burstCos = new Float32Array(count);
    this.burstSin = new Float32Array(count);
    this.burstScale = new Float32Array(count);
    for (let i = 0, k = 0; i < this.grains.length; i += 2, k++) {
      // Stable grain properties: uneven weight and friction, no random jitter.
      const seed = Math.imul(i + 1, 1597334677) >>> 0;
      this.grains[i] = 0.8 + (seed & 65535) / 65535 * 0.4;
      this.grains[i + 1] = 9 + (seed >>> 16) / 65535 * 3;
      // Immutable orbit constants, previously rehashed every substep.
      const grain = (Math.imul(i + 7, 1597334677) >>> 0) / 4294967296;
      this.orbitRadius[k] = 0.35 + grain * grain * 0.57;
      this.orbitSpin[k] = 0.8 + grain * 0.75;
      // Immutable burst directions, previously recomputed on every release.
      const burstSeed = (Math.imul(i + 13, 1597334677) >>> 0) / 4294967296;
      const angle = burstSeed * Math.PI * 2;
      this.burstCos[k] = Math.cos(angle);
      this.burstSin[k] = Math.sin(angle);
      this.burstScale[k] = 0.62 + this.grains[i] * 0.35;
    }
    this.refreshPositions();
  }
  // The engine fills the shared position array asynchronously after this
  // flow is constructed, so the scaled base positions are refreshed then.
  refreshPositions() {
    const positions = this.positions;
    for (let k = 0, p = 0; p < positions.length; k++, p += 3) {
      this.baseX[k] = positions[p] * 1.38;
      this.baseY[k] = positions[p + 1] * 1.38;
    }
  }
  pointer(x: number, y: number) {
    if (!this.active) { this.x = x; this.y = y; }
    this.targetX = x; this.targetY = y; this.active = true;
  }
  leave() { this.active = false; }
  setAspect(aspect: number) { this.viewportAspect = Math.max(0.6, Math.min(2.6, aspect)); }
  attract(x: number, y: number) {
    this.pointer(x, y);
    this.holeX = x; this.holeY = y;
    this.holding = true; this.holdSeconds = 0; this.burstAge = 100;
    this.burstGlow = 0;
  }
  release(seconds = this.holdSeconds) {
    if (!this.holding) return;
    this.holding = false;
    this.burstAge = 0; this.burstGlow = 1;
    this.settled = false; this.hasMotion = true;
    // A bounded charge makes long holds expressive without unbounded velocities.
    this.burstSpeed = 3.8 + 5.2 * (1 - Math.exp(-Math.max(0, seconds) / 2.8));
    const aspectX = this.viewportAspect * 0.8;
    const count = Math.min(this.previousCount / 2, this.burstCos.length);
    for (let k = 0, i = 0; k < count; k++, i += 2) {
      const speed = this.burstSpeed * this.burstScale[k];
      // Distributed directions fill the viewport, retaining some orbital momentum.
      this.velocities[i] = this.burstCos[k] * speed * aspectX + this.velocities[i] * 0.18;
      this.velocities[i + 1] = this.burstSin[k] * speed + this.velocities[i + 1] * 0.18;
    }
  }
  cancelAttraction() {
    this.holding = false; this.burstAge = 100; this.burstGlow = 0;
  }
  reset() {
    this.offsets.fill(0); this.velocities.fill(0); this.leave();
    this.cancelAttraction(); this.holeGlow = 0; this.holdSeconds = 0;
    this.settled = true; this.hasMotion = false;
  }
  update(delta: number, enabled: boolean, count = this.offsets.length / 2) {
    if (!enabled) { this.leave(); this.cancelAttraction(); }
    const end = Math.min(this.offsets.length, Math.max(0, Math.floor(count)) * 2);
    if (end < this.previousCount) {
      this.offsets.fill(0, end, this.previousCount);
      this.velocities.fill(0, end, this.previousCount);
    }
    this.previousCount = end;
    const totalDelta = Math.min(delta, 0.1);
    // Whole-scene rest: no attraction, return finished, cursor converged.
    // Scalar glow/cursor state advances with the same composed exponentials
    // the substeps would apply, so skipping the particle loop is exact.
    if (this.settled && !this.holding && this.burstAge >= 3 &&
      (!this.active || (this.x === this.targetX && this.y === this.targetY))) {
      this.burstAge += totalDelta;
      this.burstGlow = Math.exp(-this.burstAge * 1.35);
      this.holeGlow += (0 - this.holeGlow) * (1 - Math.exp(-totalDelta * 4));
      this.hasMotion = false;
      return;
    }
    const steps = Math.max(1, Math.ceil(delta * 240));
    const dt = totalDelta / steps;
    const radius = 0.22, radius2 = radius * radius;
    const offsets = this.offsets, velocities = this.velocities, grains = this.grains;
    const baseX = this.baseX, baseY = this.baseY;
    const orbitRadius = this.orbitRadius, orbitSpin = this.orbitSpin;
    let allSettled = true;
    for (let step = 0; step < steps; step++) {
      if (this.holding) this.holdSeconds = Math.min(30, this.holdSeconds + dt);
      else this.burstAge += dt;
      this.holeGlow += ((this.holding ? 1 : 0) - this.holeGlow) * (1 - Math.exp(-dt * (this.holding ? 3 : 4)));
      this.burstGlow = Math.exp(-this.burstAge * 1.35);
      const oldX = this.x, oldY = this.y;
      const ease = 1 - Math.exp(-24 * dt);
      if (this.active) {
        this.x += (this.targetX - this.x) * ease;
        this.y += (this.targetY - this.y) * ease;
      }
      let ux = dt > 0 ? (this.x - oldX) / dt : 0;
      let uy = dt > 0 ? (this.y - oldY) / dt : 0;
      const limit = Math.max(1, Math.sqrt(ux * ux + uy * uy) / 3);
      ux /= limit; uy /= limit;
      const cx = (this.x + oldX) * 0.5, cy = (this.y + oldY) * 0.5;
      if (this.holding) { this.holeX = this.x; this.holeY = this.y; }
      const returning = this.burstAge < 3;
      const returnProgress = Math.max(0, Math.min(1, (this.burstAge - 0.35) / 0.75));
      const returnSpring = 1 + 31 * returnProgress * returnProgress;
      const returnDrag = 0.55 + 10.0 * returnProgress;
      const orbitFollow = 1 - Math.exp(-dt * 5 * (1 - Math.exp(-this.holdSeconds * 3)));
      if (this.holding) {
        allSettled = false;
        const holeX = this.holeX, holeY = this.holeY;
        for (let k = 0, i = 0; i < end; k++, i += 2) {
          const ox = offsets[i], oy = offsets[i + 1];
          const dx = baseX[k] + ox - holeX;
          const dy = (baseY[k] + oy - holeY) * 2.5;
          const r = Math.max(0.001, Math.sqrt(dx * dx + dy * dy));
          const inv = 1 / r;
          const nx = dx * inv, ny = dy * inv;
          const inwardRaw = (r - orbitRadius[k]) * 3.2;
          const inward = inwardRaw < -1.1 ? -1.1 : inwardRaw > 1.65 ? 1.65 : inwardRaw;
          const spin = orbitSpin[k] * Math.min(1, r * 4);
          const vx = -nx * inward - ny * spin;
          const vy = (-ny * inward + nx * spin) * 0.40;
          const nextVx = velocities[i] + (vx - velocities[i]) * orbitFollow;
          const nextVy = velocities[i + 1] + (vy - velocities[i + 1]) * orbitFollow;
          velocities[i] = nextVx;
          velocities[i + 1] = nextVy;
          offsets[i] = ox + nextVx * dt;
          offsets[i + 1] = oy + nextVy * dt;
        }
        continue;
      }
      const active = this.active;
      for (let k = 0, i = 0; i < end; k++, i += 2) {
        const ox = offsets[i], oy = offsets[i + 1];
        let fx = 0, fy = 0;
        if (active) {
          const rx = baseX[k] + ox - cx;
          const ry = baseY[k] + oy - cy;
          const q = (rx * rx + ry * ry) / radius2;
          if (q < 1) {
            // Curl of a compact stream function: no outward radial evacuation.
            const s = 1 - q, g = s * s * s;
            const derivative = -6 * s * s / radius2;
            const cross = ux * ry - uy * rx;
            fx = ux * g + cross * ry * derivative;
            fy = uy * g - cross * rx * derivative;
          }
        }
        const vx0 = velocities[i], vy0 = velocities[i + 1];
        if (fx === 0 && fy === 0) {
          // Exactly at rest with no force: integration would keep it at rest.
          if (ox === 0 && oy === 0 && vx0 === 0 && vy0 === 0) continue;
          if (!returning && Math.abs(ox) + Math.abs(oy) + Math.abs(vx0) + Math.abs(vy0) < 1e-7) {
            offsets[i] = 0; offsets[i + 1] = 0; velocities[i] = 0; velocities[i + 1] = 0;
            continue;
          }
        }
        allSettled = false;
        if (returning) {
          const weight = grains[i];
          const nextVx = vx0 + (fx * 24 / weight - ox * returnSpring - vx0 * returnDrag) * dt;
          const nextVy = vy0 + (fy * 24 / weight - oy * returnSpring - vy0 * returnDrag) * dt;
          velocities[i] = nextVx;
          velocities[i + 1] = nextVy;
          offsets[i] = ox + nextVx * dt;
          offsets[i + 1] = oy + nextVy * dt;
          continue;
        }
        const weight = grains[i], friction = grains[i + 1];
        // A trace of downward settling only while displaced. The source face
        // stays still at rest; heavier grains lag behind their neighbours.
        const loosened = Math.min(1, (Math.abs(ox) + Math.abs(oy)) * 9);
        const gripX = 0.16 * vx0 / (Math.abs(vx0) + 0.06);
        const gripY = 0.16 * vy0 / (Math.abs(vy0) + 0.06);
        const nextVx = vx0 + ((fx * 24 - ox * 28) / weight - vx0 * friction - gripX) * dt;
        const nextVy = vy0 + ((fy * 24 - oy * 28) / weight - vy0 * friction - gripY - loosened * 0.7) * dt;
        velocities[i] = nextVx;
        velocities[i + 1] = nextVy;
        offsets[i] = ox + nextVx * dt;
        offsets[i + 1] = oy + nextVy * dt;
      }
    }
    this.settled = allSettled && !this.holding && this.burstAge >= 3;
    this.hasMotion = !this.settled;
  }
}
