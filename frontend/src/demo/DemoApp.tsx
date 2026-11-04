import { useCallback, useEffect, useRef, useState } from 'react';
import ParticleCanvas from '../scene/ParticleCanvas';
import PortraitSettings, { Icon } from '../PortraitSettings';
import { SoundButton } from '../AmbientSound';
import type { SceneStats } from '../scene/types';
import { visibleRun } from './presentation';

type Sample = { timestamp: string; healthy: boolean | null; memory_bytes: number | null;
  probes: {name: string; ok: boolean; status_code: number | null; duration_ms: number}[] };
type Run = { run_id: string; phase: string; verified: boolean; finished_at?: number; cleanup_complete?: boolean;
  cleanup_error?: string; error?: string; elapsed_seconds?: number; recovery_seconds?: number;
  fault_confirmed?: boolean; observation?: Sample; samples: Sample[];
  diagnosis?: { summary: string; reason: string; action: string };
  verification?: {sample_count: number; incomplete_samples: number; duration_seconds: number};
  agents?: {type: string; agent: string; summary: string}[];
  calls: {model: string; duration_ms: number; attempts: number; usage?: {total_tokens?: number}}[] };
type Status = { authenticated: boolean; public_access?: boolean; provider_ready: boolean; busy: boolean; run: Run | null;
  limits: {runs_per_visit: number; calls_per_run: number} };

const labels: Record<string, string> = {
  preparing: 'Preparing Linkding', checking_baseline: 'Checking the working app', healthy: 'Linkding is responding',
  introducing_problem: 'Changing the test port', detecting: 'Watching for the failure', queued: 'Investigation queued',
  investigating: 'Finding the cause', awaiting_approval: 'Checking permissions', executing: 'Restoring saved settings',
  verifying: 'Checking real requests', mitigated: 'Checking the final result', recovered: 'Linkding responds again',
  needs_help: 'Human review needed', needs_human: 'Human review needed', cancelled: 'Run stopped',
  interrupted: 'Run interrupted', failed: 'Recovery not confirmed',
};

function Checks({samples}: {samples: Sample[]}) {
  const points = samples.filter(s => s.timestamp && typeof s.healthy === 'boolean');
  if (points.length < 2) return null;
  const start = Date.parse(points[0].timestamp), span = Math.max(1000, Date.parse(points.at(-1)!.timestamp) - start);
  const x = (s: Sample) => 8 + (Date.parse(s.timestamp) - start) / span * 304;
  return <figure className="demo-chart"><figcaption>Measured request checks</figcaption>
    <svg viewBox="0 0 320 68" role="img" aria-label={`${points.filter(s => s.healthy).length} healthy observations out of ${points.length}`}>
      <path d="M8 12H312M8 48H312" stroke="currentColor" opacity=".12"/>
      {points.map((s, i) => <circle key={i} cx={x(s)} cy={s.healthy ? 12 : 48} r="2.5" fill={s.healthy ? '#a8daca' : '#d7a79a'}/>)}
      <text x="8" y="66">0 s</text><text x="312" y="66" textAnchor="end">{Math.round(span / 1000)} s</text>
    </svg>
    <div className="demo-chart-key"><span>Upper row: responding</span><span>Lower row: unavailable</span></div>
  </figure>;
}

export default function DemoApp() {
  const [state, setState] = useState<Status | null>(null), [connected, setConnected] = useState(false);
  const [error, setError] = useState(''), [pending, setPending] = useState(false), [code, setCode] = useState('');
  const [paused, setPaused] = useState(false), [settings, setSettings] = useState(false);
  const [quality, setQuality] = useState<'full' | 'balanced'>('balanced'), [count, setCount] = useState(60000);
  const [reduced, setReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [stats, setStats] = useState<SceneStats | null>(null);
  const submitting = useRef(false), requestKey = useRef<string | null>(null);
  const [reportId, setReportId] = useState<string | null>(null);
  const latestRun = state?.run;
  const run = visibleRun(latestRun, reportId);
  useEffect(() => {
    if (latestRun && !latestRun.finished_at) setReportId(latestRun.run_id);
  }, [latestRun]);
  const running = !!run && !run.finished_at;
  const disrupted = !!run && !run.verified && !['preparing', 'checking_baseline', 'healthy'].includes(run.phase);
  const report = useCallback((s: SceneStats) => setStats(s), []);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch('/api/demo/status', {signal, cache: 'no-store'});
    if (!response.ok) throw new Error('Live demonstration is unavailable.');
    const data: Status = await response.json();
    setState(data); setConnected(true);
  }, []);
  useEffect(() => {
    const controller = new AbortController(); let timer = 0;
    const poll = async () => {
      try { await refresh(controller.signal); } catch { if (!controller.signal.aborted) setConnected(false); }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 1500);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const change = () => setReduced(media.matches);
    media.addEventListener('change', change);
    return () => media.removeEventListener('change', change);
  }, []);
  useEffect(() => {
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') setSettings(false); };
    window.addEventListener('keydown', escape); return () => window.removeEventListener('keydown', escape);
  }, []);

  async function command(path: string, body: object = {}, key?: string) {
    if (submitting.current) return false;
    submitting.current = true; setPending(true); setError('');
    try {
      const response = await fetch(`/api/demo/${path}`, {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Aura-Demo': '1', ...(key ? {'Idempotency-Key': key} : {})},
        body: JSON.stringify(body)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || 'The request could not be completed.');
      try { await refresh(); } catch { setConnected(false); }
      return true;
    } catch (e) { setError(e instanceof Error ? e.message : 'Connection interrupted.'); return false; }
    finally { submitting.current = false; setPending(false); }
  }
  async function start() {
    // Keep the ID after a lost response; retrying cannot pay for a second run.
    requestKey.current ??= crypto.randomUUID();
    if (await command('runs', {}, requestKey.current)) requestKey.current = null;
  }
  const tokens = run?.calls.reduce((n, c) => n + (c.usage?.total_tokens ?? 0), 0) ?? 0;
  const phase = run ? labels[run.phase] || 'Checking the application' : 'Restore an unavailable app';
  const agent = run?.agents?.filter(a => a.type === 'agent_started').at(-1)?.agent;
  const annotations = [
    {name: 'Requests', detail: run?.observation?.healthy ? 'Responding' : 'Availability'},
    {name: 'Resources', detail: 'Memory and process'},
    {name: 'Linkding', detail: 'Test application'},
    {name: 'Settings', detail: 'Current and saved'},
    {name: 'Investigation', detail: 'Meta Open 1026'},
    {name: 'Verification', detail: 'Measured checks'},
  ];

  return <main className={`aura demo-page ${disrupted ? 'streams' : 'portrait'} ${reduced ? 'reduced-motion' : ''}`}>
    <ParticleCanvas settings={{mode: disrupted ? 'streams' : 'portrait', paused, reducedMotion: reduced,
      quality, resetId: 0, particleCount: count, activeNode: running ? ({runtime: 1, configuration: 3, synthesis: 4}[agent ?? ''] ?? 0) : -1}}
      onStats={report} annotations={annotations}/>
    <div className="atmosphere" aria-hidden="true"/>
    <header className="masthead"><a className="identity" href="https://github.com/GL1NoMe5/AURA" target="_blank" rel="noreferrer">AURA</a>
      <div className="demo-heading"><span className={`demo-led ${connected ? 'on' : ''}`}/>{connected ? 'Live demonstration' : 'Connecting'}<a href="https://gl1nome5.github.io/AURA/" target="_blank" rel="noreferrer">Watch the film</a></div>
    </header>
    <section className="demo-panel" aria-label="Try a real recovery">
      <div className="demo-app-name"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3h8a1 1 0 0 1 1 1v17l-5-4-5 4V4a1 1 0 0 1 1-1Z"/></svg><span>Linkding<small>Isolated test application</small></span></div>
      <h1 aria-live="polite">{phase}</h1>
      {!run && <p className="demo-intro">We’ll change a working app’s port. Watch AURA find the cause, restore its settings and check that requests work again.</p>}
      {run && <div className="demo-progress" aria-label="Recovery progress">
        {[['Test app', run.fault_confirmed || run.verified], ['Investigation', !!run.diagnosis], ['Recovery checked', run.verified]].map(([name, done]) =>
          <span key={String(name)} className={done ? 'done' : ''}><i>{done ? '✓' : ''}</i>{name}</span>)}
      </div>}
      {run?.observation && <div className="demo-probes">{run.observation.probes.map(p => <div key={p.name}>
        <span>{p.name}</span><strong className={p.ok ? 'ok' : 'bad'}>{p.status_code ?? (p.ok ? 'OK' : 'No response')}</strong>
      </div>)}</div>}
      {run?.verified && <div className="demo-result"><strong>{run.recovery_seconds?.toFixed(1)}<small>seconds to verified recovery</small></strong>
        <p>{run.verification?.sample_count ?? '—'} verification observations · {run.calls.length} model calls</p></div>}
      {run && <Checks samples={run.samples}/>}
      {run?.diagnosis?.action === 'restore_config_snapshot' && <p className="demo-diagnosis">AURA selected the saved working configuration.</p>}
      {run?.error && <p className="demo-message" role="status">{run.error}</p>}
      {run?.cleanup_error && <p className="demo-message">{run.cleanup_error}</p>}
      {!connected && state && <p className="demo-message" role="status">Connection lost. The result below is the last received update.</p>}
      {error && <p className="demo-message" role="alert">{error}</p>}
      {!state ? <div className="demo-actions"><button className="start-button" disabled>Connecting…</button></div>
      : !state.authenticated && state.public_access ? <div className="demo-actions">
        <button className="start-button" disabled={pending || !connected} onClick={() => void command('session', {code: ''})}>{pending ? 'Opening…' : 'Open demo'}</button>
        <small>No account or API key needed</small>
      </div> : !state?.authenticated ? <form className="demo-access" onSubmit={e => { e.preventDefault(); void command('session', {code}).then(ok => { if (ok) setCode(''); }); }}>
        <label htmlFor="demo-code">Demo access code</label>
        <div><input id="demo-code" type="password" autoComplete="off" value={code} onChange={e => setCode(e.target.value)} maxLength={128} required placeholder="Provided with the submission"/>
          <button type="submit" disabled={pending || !connected}>{pending ? 'Opening…' : 'Continue'}</button></div>
        <small>No API key needed</small>
      </form> : <div className="demo-actions">
        <button className="start-button" disabled={pending || !connected || !state.provider_ready || state.busy}
          onClick={() => void start()}>{pending ? 'Starting…' : running ? 'Recovery in progress' : state.busy ? 'Another run is in progress' : run ? 'Try another recovery' : 'Try a recovery'}</button>
        {running && <button className="text-button" disabled={pending} onClick={() => void command(`runs/${run.run_id}/cancel`)}>Stop this run</button>}
        {!state.provider_ready && <small>The model connection is currently unavailable.</small>}
      </div>}
      {latestRun?.finished_at && <button className="text-button" onClick={() => setReportId(run ? null : latestRun.run_id)}>
        {run ? 'Back to portrait' : 'View last result'}
      </button>}
      <p className="demo-footnote">{run?.finished_at && run.cleanup_complete ? 'Run complete. The disposable test app has been removed.' : 'Real Docker requests and Meta Open 1026. Results are measured during your run.'}</p>
      <details className="demo-details"><summary>What happens in this test</summary>
        <p>The test deliberately changes Linkding’s port. AURA receives observations and a saved working configuration. It decides whether a permitted recovery applies.</p>
        <p>This demonstrates configuration recovery. Code repair is a separate workflow that requires human approval.</p>
        <p>One recovery at a time. Up to {state?.limits.calls_per_run ?? 7} model calls, with one retry each. {state?.limits.runs_per_visit ?? 2} runs per visit. A shared usage cap also applies.</p>
        {run && <p>{tokens ? `${tokens.toLocaleString()} tokens reported by the provider.` : 'Token usage will appear when the provider reports it.'} This is not a dollar billing estimate.</p>}
        {run?.diagnosis && <p><strong>AURA’s diagnosis</strong><br/>{run.diagnosis.summary}</p>}
        {run?.agents?.filter(a => a.type === 'agent_finished').map((a, i) => <p key={i}><strong>{a.agent}</strong><br/>{a.summary}</p>)}
      </details>
    </section>
    <footer className="bottom-bar demo-bottom"><span>Move to stir · Hold to gather</span><div className="transport"><SoundButton/>
      <button className="icon-button" aria-label={paused ? 'Resume portrait animation' : 'Pause portrait animation'} aria-pressed={paused} data-tooltip="Only the portrait pauses. The recovery continues." onClick={() => setPaused(p => !p)}><Icon kind={paused ? 'play' : 'pause'}/></button>
      <button className="icon-button" aria-label="Portrait settings" aria-expanded={settings} onClick={() => setSettings(s => !s)}><Icon kind="sliders"/></button>
    </div></footer>
    {settings && <PortraitSettings count={count} onCount={setCount} quality={quality} onQuality={setQuality} reducedMotion={reduced}
      onMotion={setReduced} stats={stats} close={() => setSettings(false)}/>}
  </main>;
}
