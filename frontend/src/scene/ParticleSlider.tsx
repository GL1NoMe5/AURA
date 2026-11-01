import { useEffect, useState, type CSSProperties } from 'react';
import { DEFAULT_PARTICLES, MAX_PARTICLES, MIN_PARTICLES, PARTICLE_STEP, formatParticles } from './particleCount';

export default function ParticleSlider({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  const [active, setActive] = useState(false);
  useEffect(() => {
    const end = () => setActive(false);
    window.addEventListener('pointerup', end); window.addEventListener('pointercancel', end);
    return () => { window.removeEventListener('pointerup', end); window.removeEventListener('pointercancel', end); };
  }, []);
  const progress = (value - MIN_PARTICLES) / (MAX_PARTICLES - MIN_PARTICLES);
  return <div className={`particle-control ${active ? 'is-adjusting' : ''}`} style={{ '--particle-progress': `${progress * 100}%` } as CSSProperties}>
    <div className="particle-control-heading">
      <label htmlFor="particle-count">Particles</label>
      <output htmlFor="particle-count">{formatParticles(value)}</output>
    </div>
    <div className="particle-slider">
      <div className="particle-slider-rail" aria-hidden="true">
        <span className="particle-slider-base"/><span className="particle-slider-fill"/>
        <span className="particle-slider-grains">{Array.from({ length: 9 }, (_, i) => <i key={i} style={{ left: `${i * 12.5}%`, opacity: progress >= i / 8 ? 1 : 0.18 }}/>)}</span>
        <span className="particle-slider-thumb"><span/></span>
      </div>
      <input id="particle-count" type="range" min={MIN_PARTICLES} max={MAX_PARTICLES} step={PARTICLE_STEP} value={value}
        aria-valuetext={`${formatParticles(value)} particles`} aria-describedby="particle-count-hint"
        onChange={e => onChange(Number(e.target.value))}
        onPointerDown={() => setActive(true)} onKeyDown={() => setActive(true)} onKeyUp={() => setActive(false)} onBlur={() => setActive(false)}/>
    </div>
    <div className="particle-scale" aria-hidden="true"><span>10k</span><span>120k</span></div>
    <div className="particle-control-foot"><p id="particle-count-hint">More particles use more power.</p><button disabled={value === DEFAULT_PARTICLES} onClick={() => onChange(DEFAULT_PARTICLES)}>Reset</button></div>
  </div>;
}
