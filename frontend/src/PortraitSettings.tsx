import { SoundVolume } from './AmbientSound';
import ParticleSlider from './scene/ParticleSlider';
import type { SceneStats } from './scene/types';
import { useRef } from 'react';
import { usePanelFocus } from './usePanelFocus';

export function Icon({ kind }: { kind: 'pause' | 'play' | 'reset' | 'sliders' | 'close' }) {
  return <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {kind === 'pause' && <path d="M9 6v12M15 6v12"/>}
    {kind === 'play' && <path d="m9 5 10 7-10 7Z"/>}
    {kind === 'reset' && <><path d="M5 9a8 8 0 1 1 0 7"/><path d="M5 4v5h5"/></>}
    {kind === 'sliders' && <><path d="M4 7h16M4 17h16"/><path d="M9 4v6M15 14v6" strokeWidth="3"/></>}
    {kind === 'close' && <path d="m7 7 10 10M17 7 7 17"/>}
  </svg>;
}

export default function PortraitSettings({ count, onCount, quality, onQuality, reducedMotion, onMotion, stats, close }: {
  count: number; onCount: (value: number) => void;
  quality: 'full' | 'balanced'; onQuality: (value: 'full' | 'balanced') => void;
  reducedMotion: boolean; onMotion: (value: boolean) => void;
  stats: SceneStats | null; close: () => void;
}) {
  const panel = useRef<HTMLElement>(null);
  usePanelFocus(panel, true);
  return <section ref={panel} tabIndex={-1} className="settings-panel" id="scene-settings" aria-label="Portrait settings">
    <div className="panel-heading"><h2>Settings</h2><button className="icon-button" aria-label="Close settings" onClick={close}><Icon kind="close"/></button></div>
    <ParticleSlider value={count} onChange={onCount}/>
    <div className="setting-row"><span id="quality-label">Detail</span><div className="quality-options" data-quality={quality} role="group" aria-labelledby="quality-label">
      <button aria-pressed={quality === 'balanced'} onClick={() => onQuality('balanced')}>Balanced</button>
      <button aria-pressed={quality === 'full'} onClick={() => onQuality('full')}>High</button>
    </div></div>
    <label className="setting-row">Less motion<input className="quiet-switch" type="checkbox" role="switch" checked={reducedMotion} onChange={e => onMotion(e.target.checked)}/></label>
    <SoundVolume/>
    <details className="render-details"><summary>Performance</summary><dl className="stats">
      <div><dt>Frame rate</dt><dd>{stats ? `${stats.fps} fps` : '—'}</dd></div>
      <div><dt>Resolution</dt><dd>{stats ? `${stats.width} × ${stats.height}` : '—'}</dd></div>
      <div><dt>CPU time per frame</dt><dd>{stats ? `${stats.cpuMs} ms` : '—'}</dd></div>
    </dl></details>
  </section>;
}
