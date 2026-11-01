import { SoundButton } from './AmbientSound';
import { useCallback, useEffect, useRef, useState } from 'react';
import ParticleCanvas from './scene/ParticleCanvas';
import PortraitSettings, { Icon as Symbol } from './PortraitSettings';
import { DEFAULT_PARTICLES, normalizeParticleCount } from './scene/particleCount';
import type { SceneMode, SceneStats } from './scene/types';
import { useRuntime } from './runtime/useRuntime';
import { activePhase, sceneForState } from './runtime/model';
import { AgentDetails, LiveMetrics, mib, phaseLabels } from './runtime/LivePanels';
import { RenderCheck } from './scene/renderCheck';
import OpsApp from './ops/OpsApp';
import DemoApp from './demo/DemoApp';
import QuickTour from './QuickTour';
import '@fontsource/manrope/latin-400.css';
import '@fontsource/manrope/latin-500.css';
import '@fontsource/manrope/latin-600.css';

export default function App() {
  if (location.pathname === '/try' || location.pathname === '/try/') return <DemoApp/>;
  const query = new URLSearchParams(location.search);
  return query.has('preview') || query.get('demo') === '1' ? <LegacyApp/> : <OpsApp/>;
}

function LegacyApp() {
  const [preview, setPreview] = useState(() => new URLSearchParams(location.search).get('preview') === '1');
  const [renderCheck] = useState(() => {
    const seconds = Number(new URLSearchParams(location.search).get('renderCheck'));
    return preview && Number.isFinite(seconds) && seconds > 0 ? new RenderCheck(seconds) : null;
  });
  const live = useRuntime(!preview);
  const [agentsOpen, setAgentsOpen] = useState(false);
  const mode: SceneMode = preview && renderCheck && !renderCheck.report ? renderCheck.mode : preview ? 'portrait' : live.pending === 'reset' ? 'streams' : sceneForState(live.state);
  const phase = live.state?.phase ?? 'idle';
  const busy = activePhase(phase);
  const recovered = !preview && phase === 'resolved' && live.state?.latest_verification?.verified === true && !live.state.simulated_diagnosis;
  const connected = live.connection === 'connected';
  const [paused, setPaused] = useState(false), [resetId, setResetId] = useState(0);
  const [quality, setQuality] = useState<'full' | 'balanced'>('full');
  const [particleCount, setParticleCount] = useState(() => {
    try { const saved = localStorage.getItem('aura.particleCount'); return saved === null ? DEFAULT_PARTICLES : normalizeParticleCount(Number(saved)); }
    catch { return DEFAULT_PARTICLES; }
  });
  useEffect(() => { try { localStorage.setItem('aura.particleCount', String(particleCount)); } catch { /* Storage is optional. */ } }, [particleCount]);
  const [reducedMotion, setReducedMotion] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [details, setDetails] = useState(false), [error, setError] = useState('');
  const [stats, setStats] = useState<SceneStats | null>(null);
  const sceneState = useRef({ paused, reducedMotion, quality, mode, particleCount });
  sceneState.current = { paused, reducedMotion, quality, mode, particleCount };
  const report = useCallback((s: SceneStats) => { renderCheck?.add(s, sceneState.current, sceneState.current.mode); setStats(s); }, [renderCheck]);
  const fail = useCallback((s: string) => setError(s), []);
  useEffect(() => {
    const visibility = () => { if (document.hidden && renderCheck && !renderCheck.report) renderCheck.issues.add('Tab hidden during check'); };
    document.addEventListener('visibilitychange', visibility);
    return () => document.removeEventListener('visibilitychange', visibility);
  }, [renderCheck]);
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const changed = () => setReducedMotion(media.matches);
    media.addEventListener('change', changed);
    return () => media.removeEventListener('change', changed);
  }, []);
  useEffect(() => {
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { setDetails(false); setAgentsOpen(false); } };
    window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key);
  }, []);
  function reset() {
    setPaused(false); setResetId(id => id + 1);
    if (!preview) void live.reset();
  }
  return <main className={`aura ${mode} ${preview ? 'visual-preview' : ''} ${reducedMotion ? 'reduced-motion' : ''}`}>
    <h1 className="sr-only">AURA</h1>
    <ParticleCanvas settings={{ mode, paused, reducedMotion, quality, resetId, particleCount }} onStats={report} onError={fail}/>
    <div className="atmosphere" aria-hidden="true"/>
    {renderCheck && <aside className="render-check" aria-label="Render check">
      <strong>{renderCheck.report ? 'Render check complete' : 'Render check running'}</strong>
      <p>{Math.floor(renderCheck.seconds)} / {renderCheck.target} measured seconds</p>
      <p>{renderCheck.target < 1200 ? 'Short trial · not final acceptance' : '20-minute check · keep this tab visible'}</p>
      {renderCheck.report && <><p>{Number(renderCheck.report.average_fps).toFixed(1)} FPS average</p><button onClick={() => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(renderCheck.report, null, 2)], { type: 'application/json' }));
        const a = document.createElement('a'); a.href = url; a.download = 'aura-render-check.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }}>Download report</button></>}
    </aside>}
    <header className="masthead">
      <div className="identity"><svg width="36" height="36" viewBox="0 0 36 36" fill="none" aria-hidden="true"><circle cx="18" cy="18" r="15" stroke="currentColor" strokeOpacity=".3"/><ellipse cx="18" cy="18" rx="8" ry="15" stroke="currentColor" strokeOpacity=".65" transform="rotate(34 18 18)"/><circle cx="18" cy="18" r="3" fill="currentColor"/></svg><span>AURA</span></div>
      <div className="session-controls"><div className={`preview-badge ${!preview && !connected ? 'disconnected' : ''}`}><i/>{preview ? 'Offline preview' : connected ? 'Live service' : live.connection === 'connecting' ? 'Connecting' : 'Reconnecting'}{!preview && <span>Demo service</span>}</div>
        <button data-tour="return" className="text-button" disabled={!preview && (busy || !!live.pending)} onClick={() => { if (preview) { location.assign('/'); return; } const next = !preview; const url = new URL(location.href); if (next) url.searchParams.set('preview', '1'); else url.searchParams.delete('preview'); history.replaceState(null, '', url); setPreview(next); setPaused(false); setResetId(id => id + 1); setAgentsOpen(false); }}>{preview ? 'Back to apps' : 'Preview'}</button>
        {preview && <QuickTour variant="preview" autoStart={!renderCheck && !details} onStart={() => { setDetails(false); setAgentsOpen(false); }}/>}</div>
    </header>
    {!preview && <div className="scene-caption" aria-live="polite"><span className="status-line"/><span>{preview ? 'Interactive portrait / Offline preview' : live.pending === 'reset' ? 'Resetting service' : !connected ? 'Waiting for service connection' : phaseLabels[phase]}</span></div>}
    {!preview && <LiveMetrics telemetry={live.state?.telemetry} samples={live.samples} connected={connected}/>}
    {!preview && (live.error || live.state?.error || (!connected && live.connection === 'reconnecting')) && <div className="run-notice" role="status">{live.error || live.state?.error?.message || 'Connection lost. Start AURA and keep this page open; reconnection is automatic.'}</div>}
    {!preview && connected && live.health && !live.health.orchestrator_ready && <div className="run-notice" role="status">Diagnosis unavailable. Configure the local Baseten key, then restart AURA.</div>}
    {error && <div className="error-panel" role="alert"><strong>Scene unavailable</strong><p>{error}</p><button onClick={() => location.reload()}>Reload scene</button></div>}

    {!preview && <aside className="scene-note"><span className="note-index">{preview ? 'Interactive portrait' : phaseLabels[phase]}</span><p>{recovered ? <>{mib(live.state?.latest_verification?.retained_cache_bytes)} cache<br/>{live.state?.latest_verification?.duration_seconds.toFixed(2)} s verified</> : mode === 'portrait' ? <>A presence,<br/>made of light.</> : <>Tracing the fault.<br/>Restoring the flow.</>}</p><span className="portrait-note">{preview ? stats?.portraitReady ? 'Touch the light. It finds its way back.' : 'Preparing portrait' : `${live.state?.agents.filter(a => a.status === 'completed').length ?? 0} / 5 agents completed`}</span></aside>}
    <footer className="bottom-bar">
      {preview ? null : <div className="incident-controls"><button className="start-button" disabled={!live.canStart || !stats?.portraitReady} onClick={() => { setPaused(false); void live.start(); }}>{live.pending === 'start' ? 'Starting…' : busy ? phaseLabels[phase] : 'Start incident'}</button><button className="reset-button" disabled={!!live.pending || !live.state} onClick={reset}>{live.pending === 'reset' ? 'Resetting…' : busy ? 'Cancel & reset' : 'Reset'}</button><button className={`text-button ${agentsOpen ? 'active' : ''}`} aria-expanded={agentsOpen} onClick={() => { setAgentsOpen(a => !a); setDetails(false); }}>Agents & journal</button></div>}
      <div data-tour="controls" className="transport"><span className="sr-only" id="animation-pause-help">{preview ? 'Only the portrait pauses.' : 'Only the portrait pauses. App monitoring continues.'}</span><SoundButton/><button className="icon-button portrait-pause" data-tooltip={preview ? 'Pause or resume portrait animation.' : 'Portrait animation only. Monitoring continues.'} aria-pressed={paused} aria-label={paused ? 'Resume portrait animation' : 'Pause portrait animation'} aria-describedby="animation-pause-help" onClick={() => setPaused(p => !p)}><Symbol kind={paused ? 'play' : 'pause'}/></button>{preview && <button className="icon-button" aria-label="Reset scene" onClick={reset}><Symbol kind="reset"/></button>}<span className="transport-divider"/><button className={`icon-button ${details ? 'active' : ''}`} aria-label="Portrait settings" aria-expanded={details} aria-controls="scene-settings" onClick={() => { setDetails(d => !d); setAgentsOpen(false); }}><Symbol kind="sliders"/></button></div>

    </footer>
    {details && <PortraitSettings count={particleCount} onCount={v => setParticleCount(normalizeParticleCount(v))} quality={quality} onQuality={setQuality} reducedMotion={reducedMotion} onMotion={setReducedMotion} stats={stats} close={() => setDetails(false)}/>}
    {!preview && agentsOpen && <AgentDetails runtime={live} close={() => setAgentsOpen(false)}/>}
  </main>;
}
