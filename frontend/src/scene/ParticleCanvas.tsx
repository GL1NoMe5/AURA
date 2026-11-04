import { useEffect, useRef, useState } from 'react';
import { createScene, NODES } from './engine';
import type { SceneSettings, SceneStats } from './types';
import './portraitLoading.css';

const ignoreError = (_message: string) => {};

export default function ParticleCanvas({ settings, onStats, onError = ignoreError, annotations }: { settings: SceneSettings; onStats: (s: SceneStats) => void; onError?: (s: string) => void; annotations?: { name: string; detail: string; tone?: string }[] }) {
  const host = useRef<HTMLDivElement>(null);
  const labels = useRef<HTMLDivElement[]>([]);
  const latest = useRef(settings); latest.current = settings;
  const [load, setLoad] = useState({ loaded: 0, total: 120000 });
  const [error, setError] = useState(''), [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!host.current) return;
    setLoad({ loaded: 0, total: 120000 }); setError('');
    const fail = (message: string) => { setError(message); onError(message); };
    try { return createScene(host.current, labels.current, () => latest.current, onStats, fail,
      (loaded, total) => setLoad({ loaded, total })); }
    catch { fail('WebGL 2 is unavailable. Open AURA in a browser with hardware acceleration enabled.'); }
  }, [onStats, onError, attempt]);
  return <div className="scene" ref={host}>
    {(load.loaded < load.total || error) && <div className="portrait-loading" onPointerDown={e => e.stopPropagation()}>
      <span role="status">{error ? 'Portrait could not finish loading' : 'Loading portrait'}</span>
      {!error && <progress aria-label="Portrait loading" value={load.loaded} max={load.total}/>}
      {error ? <button onClick={() => { onError(''); setAttempt(n => n + 1); }}>Retry portrait</button>
        : <small aria-hidden="true">{Math.floor(load.loaded / load.total * 100)}%</small>}
    </div>}
    {NODES.map((n, i) => <div key={i} aria-hidden={settings.mode === 'portrait'} ref={el => { if (el) labels.current[i] = el; }} className={`node-label ${annotations ? `node-label--${annotations[i]?.tone ?? 'quiet'}` : settings.mode === 'streams' && i === 4 ? 'node-label--fault' : ''}`}>
      <span>{annotations?.[i]?.name ?? n.name}</span><small>{annotations?.[i]?.detail ?? n.detail}</small>
    </div>)}
  </div>;
}
