import type { SceneMode, SceneStats } from './types';

type Sample = { mode: SceneMode; seconds: number; frames: number; cpuMs: number; geometries: number; textures: number; heapBytes: number | null; width: number; height: number };
export class RenderCheck {
  readonly target: number;
  readonly issues = new Set<string>();
  readonly samples: Sample[] = [];
  seconds = 0;
  warmup = 0;
  report: Record<string, unknown> | null = null;
  constructor(seconds: number) { this.target = Math.max(10, Math.min(1200, seconds)); }
  get mode(): SceneMode { return (['portrait', 'streams'] as const)[Math.floor(this.seconds / 20) % 2]; }
  add(stats: SceneStats, settings: { paused: boolean; reducedMotion: boolean; quality: string }, mode: SceneMode) {
    if (this.report) return;
    if (!stats.portraitReady) return;
    if (this.warmup < 5) { this.warmup += stats.seconds; return; }
    if (settings.paused) this.issues.add('Animation paused');
    if (settings.reducedMotion) this.issues.add('Reduced motion enabled');
    if (settings.quality !== 'full') this.issues.add('Quality changed from Full');
    if (stats.points !== 60000) this.issues.add('Particle count changed from 60,000');
    this.samples.push({ mode, seconds: stats.seconds, frames: stats.frames, cpuMs: stats.cpuMs, geometries: stats.geometries, textures: stats.textures, heapBytes: stats.heapBytes, width: stats.width, height: stats.height });
    this.seconds += stats.seconds;
    if (this.seconds < this.target) return;
    const totals = (rows: Sample[]) => ({ seconds: rows.reduce((n, s) => n + s.seconds, 0), frames: rows.reduce((n, s) => n + s.frames, 0) });
    const byMode = Object.fromEntries((['portrait', 'streams'] as const).map(mode => {
      const rows = this.samples.filter(s => s.mode === mode), sum = totals(rows);
      return [mode, { ...sum, average_fps: sum.seconds ? sum.frames / sum.seconds : null }];
    }));
    const total = totals(this.samples);
    const geometries = this.samples.map(s => s.geometries), textures = this.samples.map(s => s.textures);
    const stableResources = Math.max(...geometries) === Math.min(...geometries) && Math.max(...textures) === Math.min(...textures);
    const resolution = this.samples.every(s => s.width === 1920 && s.height === 1080);
    const everyMode = Object.values(byMode).every(s => s.average_fps != null && s.average_fps >= 55);
    this.report = {
      kind: this.target >= 1200 ? '20-minute-render-check' : 'short-render-trial',
      completed_at: new Date().toISOString(), requested_seconds: this.target, measured_seconds: this.seconds,
      average_fps: total.frames / total.seconds, by_mode: byMode, gpu: stats.gpu, points: stats.points,
      fixed_gpu_resources: stableResources, geometry_range: [Math.min(...geometries), Math.max(...geometries)], texture_range: [Math.min(...textures), Math.max(...textures)],
      full_hd: resolution, issues: [...this.issues],
      acceptance_pass: this.target >= 1200 && resolution && stableResources && everyMode && !this.issues.size,
      limitations: ['GPU counters are resource counts, not memory bytes.', 'JS heap samples require review for GC/continuous growth; this check does not prove absence of all leaks.', 'Short trials are not the 20-minute acceptance.'],
      samples: this.samples,
    };
  }
}
