import { useEffect, useRef } from 'react';
import { createScene, NODES } from './engine';
import type { SceneSettings, SceneStats } from './types';

export default function ParticleCanvas({ settings, onStats, onError, annotations }: { settings: SceneSettings; onStats: (s: SceneStats) => void; onError: (s: string) => void; annotations?: { name: string; detail: string; tone?: string }[] }) {
  const host = useRef<HTMLDivElement>(null);
  const labels = useRef<HTMLDivElement[]>([]);
  const latest = useRef(settings); latest.current = settings;
  useEffect(() => {
    if (!host.current) return;
    try { return createScene(host.current, labels.current, () => latest.current, onStats, onError); }
    catch { onError('WebGL 2 is unavailable. Open AURA in a browser with hardware acceleration enabled.'); }
  }, [onStats, onError]);
  return <div className="scene" ref={host}>
    {NODES.map((n, i) => <div key={i} aria-hidden={settings.mode === 'portrait'} ref={el => { if (el) labels.current[i] = el; }} className={`node-label ${annotations ? `node-label--${annotations[i]?.tone ?? 'quiet'}` : settings.mode === 'streams' && i === 4 ? 'node-label--fault' : ''}`}>
      <span>{annotations?.[i]?.name ?? n.name}</span><small>{annotations?.[i]?.detail ?? n.detail}</small>
    </div>)}
  </div>;
}
