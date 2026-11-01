import { GENERAL_CHECK, sceneSteps } from './sceneSteps';
import { checkBlockReason, connectionSpec, observationLabel, fixReviewAvailable, fixPreparationAvailable } from './workflow';
import { SoundButton } from '../AmbientSound';
import { usePanelFocus } from '../usePanelFocus';
import { useCallback, useEffect, useRef, useState } from 'react';
import ParticleCanvas from '../scene/ParticleCanvas';
import PortraitSettings, { Icon } from '../PortraitSettings';
import { DEFAULT_PARTICLES, normalizeParticleCount } from '../scene/particleCount';
import type { SceneMode, SceneStats } from '../scene/types';
import { ongoing, phaseLabel, useOps, type Investigation, type Proposal } from './useOps';
import './ops.css';
import './connections.css';
import ConnectionSettings from './ConnectionSettings';
import { currentConnection, newConnectionId } from './connectionProfile';
import PatchDiff from './PatchDiff';
import Reconciliation from './Reconciliation';
import CheckStory, { Mark, resultLabel } from './CheckStory';
import QuickTour from '../QuickTour';
import TargetPolicy from './TargetPolicy';

const size = (n?: number | null) => n == null ? '—' : `${(n / 1048576).toFixed(1)} MiB`;
const actionName = (name: string) => ({ restart_container: 'Restart the app', restore_config_snapshot: 'Restore working settings', rollback_image: 'Return to a previous version', deploy_verified_patch: 'Apply the tested fix' })[name] ?? 'Review this change';
const tabs = { applications: 'My apps', investigations: 'Activity', actions: 'Review' };
const actionHelp = (name: string) => ({ restart_container: 'Stop and start this app once. This may briefly interrupt it.', restore_config_snapshot: 'Bring back saved working settings. The app may briefly restart.', rollback_image: 'Switch to the saved earlier version. The app may briefly restart.', deploy_verified_patch: 'Install the tested correction, then check the real app. This may briefly interrupt it.' })[name] ?? 'Review the technical details before allowing this change.';

export default function OpsApp() {
  const ops = useOps();
  const [panel, setPanel] = useState<'applications' | 'investigations' | 'actions' | null>(null);
  const [settings, setSettings] = useState(false), [selected, setSelected] = useState('');
  const [paused, setPaused] = useState(false), [quality, setQuality] = useState<'full' | 'balanced'>('full');
  const [reducedMotion, setReducedMotion] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [particleCount, setParticleCount] = useState(() => { try { const v = localStorage.getItem('aura.particleCount'); return v ? normalizeParticleCount(Number(v)) : DEFAULT_PARTICLES; } catch { return DEFAULT_PARTICLES; } });
  const [stats, setStats] = useState<SceneStats | null>(null), [sceneError, setSceneError] = useState('');
  const [enrolling, setEnrolling] = useState(false), [name, setName] = useState(''), [container, setContainer] = useState(''), [url, setUrl] = useState(''), [context, setContext] = useState('default');
  const [question, setQuestion] = useState(''), [expanded, setExpanded] = useState<string | null>(null), [importError, setImportError] = useState('');
  const [asking, setAsking] = useState(false), [showComposer, setShowComposer] = useState(false);
  const [notice, setNotice] = useState(''), [allHistory, setAllHistory] = useState(false);
  const checkForm = useRef<HTMLFormElement>(null);
  const connectionName = useRef<HTMLInputElement>(null), errorMessage = useRef<HTMLParagraphElement>(null);
  const questionInput = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null), panelRef = useRef<HTMLElement>(null);
  const targets = ops.state?.targets ?? [];
  const watching = targets.some(t => t.monitoring);
  const connectionLabel = ops.connection !== 'connected'
    ? ops.connection === 'connecting' ? 'Connecting…' : 'Reconnecting…'
    : !targets.length ? 'No apps connected' : !watching ? 'Watching paused' : 'Watching';
  const target = targets.find(t => t.target_id === selected) ?? targets.find(t => t.target_id === ops.state?.investigations[0]?.target_id) ?? targets[0];
  const runs = (ops.state?.investigations ?? []).filter(r => !target || r.target_id === target.target_id);
  const current = runs.find(run => currentConnection(run, target));
  const historyRuns = allHistory ? ops.state?.investigations ?? [] : runs;
  const busy = !!current && ongoing.has(current.phase);
  const pendingApprovals = (ops.state?.proposals ?? []).filter(p => p.status === 'proposed');
  const blockedTargets = targets.filter(t => t.reconciliation_required);
  const targetApprovals = pendingApprovals.filter(p => p.target_id === target?.target_id);
  const blockedCheck = checkBlockReason(ops.state, ops.connection, target);
  const observedHealthy = target?.observation?.healthy;
  const mode: SceneMode = busy || fixReviewAvailable(current) || observedHealthy === false ? 'streams' : 'portrait';
  const steps = sceneSteps(target, current, ops.state?.proposals ?? [], ops.connection === 'connected');
  const activeNode = steps.findIndex(step => step.tone === 'active');
  const statusText = current && ongoing.has(current.phase) ? phaseLabel[current.phase] : !target ? 'Ready to connect' : observationLabel(target, ops.connection);
  const report = useCallback((value: SceneStats) => setStats(value), []);
  const failure = useCallback((value: string) => setSceneError(value), []);
  useEffect(() => { try { localStorage.setItem('aura.particleCount', String(particleCount)); } catch {} }, [particleCount]);
  usePanelFocus(panelRef, panel !== null);
  useEffect(() => { if (enrolling && panel === 'applications') connectionName.current?.focus(); }, [enrolling, panel]);
  useEffect(() => { if (ops.error || importError) errorMessage.current?.focus(); }, [ops.error, importError]);
  useEffect(() => { const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { setPanel(null); setSettings(false); } }; window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key); }, []);
  useEffect(() => { const media = matchMedia('(prefers-reduced-motion: reduce)'); const update = () => setReducedMotion(media.matches); media.addEventListener('change', update); return () => media.removeEventListener('change', update); }, []);
  useEffect(() => { if (asking && panel === 'investigations') questionInput.current?.focus(); }, [asking, panel]);
  useEffect(() => {
    if (showComposer && panel === 'investigations') {
      checkForm.current?.scrollIntoView({ block: 'start' });
      checkForm.current?.focus({ preventScroll: true });
      setShowComposer(false);
    }
  }, [showComposer, panel]);
  const open = (next: typeof panel) => { setPanel(panel === next ? null : next); setSettings(false); };
  async function enroll() {
    setImportError('');
    try {
      const spec = connectionSpec(name, container, url, context, targets.map(t => t.target_id));
      spec.target_id = newConnectionId(spec.target_id);
      const result = await ops.command('targets', spec);
      if (result) { setSelected(result.target_id); setEnrolling(false); setName(''); setContainer(''); setUrl(''); setNotice('Connected. AURA is watching; automatic changes are off.'); requestAnimationFrame(() => panelRef.current?.focus()); }
    } catch (error) { setImportError(error instanceof Error ? error.message : 'Check the connection details.'); }
  }

  async function importTarget(file?: File) {
    if (!file) return;
    setImportError('');
    try {
      if (file.size > 256 * 1024) throw new Error('Choose a connection file smaller than 256 KB.');
      const spec = JSON.parse(await file.text());
      if (spec && targets.some(t => t.target_id === spec.target_id)) {
        setSelected(spec.target_id); setEnrolling(false);
        setNotice('Already connected. Pause watching, then choose Edit connection to load this setup.');
        return;
      }
      const result = await ops.command('targets', spec);
      if (result) { setSelected(result.target_id); setEnrolling(false); }
    } catch (error) { setImportError(error instanceof SyntaxError ? 'This file is not valid JSON. Choose the connection file for your app.' : error instanceof Error ? error.message : 'Could not read the connection file.'); }
    finally { if (fileInput.current) fileInput.current.value = ''; }
  }
  async function investigate() {
    if (!target || blockedCheck || busy || ops.pending) return;
    const result = await ops.command('investigations', { target_id: target.target_id, question: question.trim() || GENERAL_CHECK });
    if (result) { setExpanded(result.investigation_id); setPanel('investigations'); setQuestion(''); setAsking(false); }
  }
  async function decide(proposal: Proposal, approve: boolean) {
    const result = await ops.command(`proposals/${proposal.proposal_id}/decision`, { proposal_hash: proposal.proposal_hash, approve });
    if (result) {
      setSelected(proposal.target_id);
      setExpanded(proposal.investigation_id);
      setPanel('investigations');
    }
  }
  function proposalChanges(proposal: Proposal) {
    const patch = ops.state?.investigations.find(run => run.investigation_id === proposal.investigation_id)?.patch;
    return patch?.patch_id === proposal.patch_id && patch?.artifact?.artifact_id
      ? <PatchDiff key={patch.artifact.artifact_id} artifactId={patch.artifact.artifact_id}/>
      : <p className="ops-small">The changes are unavailable here. Check the saved fix in Activity before allowing installation.</p>;
  }
  function runDetails(run: Investigation) {
    const runTarget = targets.find(t => t.target_id === run.target_id);
    const isCurrent = currentConnection(run, runTarget);
    const deployment = ops.state?.proposals.find(p => p.investigation_id === run.investigation_id && p.action === 'deploy_verified_patch' && p.status === 'verified');
    const baselineSaved = run.baseline_promoted || (deployment && runTarget?.verified_deployment?.proposal_id === deployment.proposal_id);
    return <div className="ops-run-detail">
      <CheckStory run={run}/>
      {!isCurrent && <p className="connection-history-note">{runTarget ? 'From earlier connection settings. Start a new check to make a change.' : 'Saved result from a removed connection.'}</p>}
      {run.trigger === 'monitor' ? <p className="ops-context-note">AURA noticed a problem while watching.</p> : run.question && run.question !== GENERAL_CHECK && <p className="ops-context-note"><span>Your note</span>{run.question}</p>}
      {run.baseline_promoted && <p className="saved-note"><Mark/>Working version saved</p>}
      {run.baseline_error && <p className="ops-error">The fix works, but its version record needs attention.</p>}
      {isCurrent && <div className="ops-inline-actions">
        {run.phase === 'verified_fixed' && deployment && !baselineSaved && <button disabled={ops.pending || !!ops.state?.active_id} onClick={() => void ops.command(`deployments/${deployment.proposal_id}/promote`)}>Save this version</button>}
        {fixReviewAvailable(run) && <button className="ops-approve" disabled={ops.pending} onClick={() => void ops.command('deployments', { investigation_id: run.investigation_id })}>Review fix</button>}
        {run.phase === 'awaiting_approval' && <button className="ops-approve" onClick={() => setPanel('actions')}>Review change</button>}
        {ongoing.has(run.phase) ? <button disabled={ops.pending} onClick={() => void ops.command(`investigations/${run.investigation_id}/cancel`)}>Stop this check</button> : fixPreparationAvailable(run) && <button disabled={ops.pending || !runTarget?.patch_profile} onClick={() => void ops.command('patches', { investigation_id: run.investigation_id })}>Test a fix</button>}
      </div>}
      <details className="ops-technical"><summary>Details</summary>
        <p>{run.diagnosis?.summary ?? run.question}</p>
        {run.error && <p className="ops-error">{run.error.message}</p>}
        {run.baseline_error && <p className="ops-error">{run.baseline_error.message}</p>}
        {run.diagnosis?.findings?.map((finding, i) => <div className="ops-finding" key={i}><small>{finding.confidence}</small><p>{finding.hypothesis}</p><span>{finding.evidence_refs.join(', ')}</span></div>)}
        {run.verification && <p>{run.verification.verified ? `Availability checked for ${(run.verification.duration_seconds ?? 0).toFixed(1)} seconds.` : run.verification.reason ?? 'Recovery not verified.'}</p>}
        {run.calls && <p className="ops-small">{run.calls.length} model calls · {run.calls.reduce((total, call) => total + (call.usage?.total_tokens ?? 0), 0).toLocaleString()} reported tokens{run.calls.some(c => !c.usage) ? ' (partial usage)' : ''}</p>}
        {run.patch?.artifact?.artifact_id && <PatchDiff artifactId={run.patch.artifact.artifact_id}/>}
        {!!run.verification && <details className="ops-evidence"><summary>Verification measurements</summary><pre>{JSON.stringify(run.verification, null, 2)}</pre></details>}
        {!!run.evidence && <details className="ops-evidence"><summary>Original measurements</summary><pre>{JSON.stringify(run.evidence, null, 2)}</pre></details>}
      </details>
    </div>;
  }
  return <main className={`aura ops-app ${mode} ${reducedMotion ? 'reduced-motion' : ''} ${panel ? 'has-workspace' : ''}`}>
    <h1 className="sr-only">AURA</h1>
    <ParticleCanvas annotations={steps} settings={{ mode, paused, reducedMotion, quality, resetId: 0, particleCount, activeNode }} onStats={report} onError={failure}/>
    <div className="atmosphere" aria-hidden="true"/>
    <header className="masthead"><div className="identity"><svg width="36" height="36" viewBox="0 0 36 36" fill="none" aria-hidden="true"><circle cx="18" cy="18" r="15" stroke="currentColor" strokeOpacity=".3"/><ellipse cx="18" cy="18" rx="8" ry="15" stroke="currentColor" strokeOpacity=".65" transform="rotate(34 18 18)"/><circle cx="18" cy="18" r="3" fill="currentColor"/></svg><span>AURA</span></div><div className="session-controls"><div className={`preview-badge ${ops.connection !== 'connected' ? 'disconnected' : !watching ? 'idle' : ''}`} role="status"><i/>{connectionLabel}{targets.length > 0 && <span>{targets.length} {targets.length === 1 ? 'application' : 'applications'}</span>}</div><a className="text-button" href="/?preview=1">Preview</a><QuickTour variant="ops" autoStart={ops.connection !== 'connecting' && !current && !pendingApprovals.length && !panel && !settings} onStart={() => { setSettings(false); setPanel(null); }} onComplete={() => { setSettings(false); setPanel('applications'); }}/></div></header>
    <div className="scene-caption" aria-live="polite"><span className="status-line"/><span>{statusText}</span></div>
    {target && <div className="live-metrics ops-metrics"><button onClick={() => open('applications')} className="ops-target-name">{target.name}<span>⌄</span></button>
      <div className="app-health"><i className={ops.connection === 'connected' && target.monitoring ? observedHealthy === true ? 'good' : observedHealthy === false ? 'bad' : '' : ''}/><span>{observationLabel(target, ops.connection)}</span></div>
      <div className="memory-caption"><span>Memory in use</span><strong>{target.observation?.resources?.memory_bytes != null && target.observation?.resources?.memory_limit_bytes ? `${Math.round(target.observation.resources.memory_bytes / target.observation.resources.memory_limit_bytes * 100)}%` : '—'}</strong></div>
      <div className="memory-gauge" role="meter" aria-label="Memory in use" aria-valuemin={0} aria-valuemax={100} aria-valuenow={target.observation?.resources?.memory_bytes != null && target.observation?.resources?.memory_limit_bytes ? Math.min(100, Math.round(target.observation.resources.memory_bytes / target.observation.resources.memory_limit_bytes * 100)) : undefined}><span style={{ width: `${target.observation?.resources?.memory_bytes != null && target.observation?.resources?.memory_limit_bytes ? Math.min(100, target.observation.resources.memory_bytes / target.observation.resources.memory_limit_bytes * 100) : 0}%` }}/></div>
      <p className="control-note">{target.reconciliation_required ? 'Changes on hold' : target.policy.automatic_actions.length ? 'Only changes you allow' : 'Changes need your permission'}</p></div>}

    {((!panel && ops.error) || sceneError) && <div className="run-notice" role="status">{ops.error || sceneError}</div>}

    {(current || pendingApprovals.length > 0) && <aside className="scene-note ops-last-check"><span className="note-index">{pendingApprovals.length ? 'Waiting for you' : current ? 'Last check' : 'Activity'}</span><p>{pendingApprovals.length ? 'A change to review.' : current ? resultLabel(current) : 'No checks yet.'}</p>{(current || pendingApprovals.length > 0) && <button className="text-button" onClick={() => { setPanel(pendingApprovals.length ? 'actions' : 'investigations'); setExpanded(current?.investigation_id ?? null); }}>View {pendingApprovals.length ? 'change' : 'result'}</button>}</aside>}
    <footer className="bottom-bar"><div className="incident-controls"><button data-tour="check" className="start-button" title={target?.reconciliation_required ? 'Read current state without a model call' : target && !busy && !targetApprovals.length && !fixReviewAvailable(current) ? 'Add context and review before starting a check' : undefined} disabled={ops.pending || !ops.state || ops.connection !== 'connected'} onClick={() => {
        setAllHistory(false);
        if (target?.reconciliation_required || targetApprovals.length) open('actions');
        else if (current && (busy || fixReviewAvailable(current))) { setPanel('investigations'); setSettings(false); setExpanded(current.investigation_id); }
        else if (target) {
          setPanel('investigations'); setSettings(false); setExpanded('');
          setShowComposer(true);
        }
        else { setPanel('applications'); setSettings(false); setEnrolling(true); }
      }}>{ops.pending ? 'Please wait…' : !ops.state || ops.connection !== 'connected' ? ops.connection === 'disconnected' ? 'Offline' : 'Connecting…' : target?.reconciliation_required ? 'Review app state' : targetApprovals.length || fixReviewAvailable(current) ? 'Review change' : busy ? 'View progress' : target ? 'Check app' : 'Connect an app'}</button><button data-tour="apps" className={`text-button ${panel === 'applications' ? 'active' : ''}`} aria-expanded={panel === 'applications'} aria-controls="ops-workspace" onClick={() => open('applications')}>My apps</button><button data-tour="activity" className={`text-button ${panel === 'investigations' ? 'active' : ''}`} aria-expanded={panel === 'investigations'} aria-controls="ops-workspace" onClick={() => open('investigations')}>Activity</button>{pendingApprovals.length > 0 && <button className="text-button ops-action-count" onClick={() => open('actions')}>Review · {pendingApprovals.length}</button>}</div><div data-tour="controls" className="transport"><span className="sr-only" id="animation-pause-help">Only the portrait pauses. App monitoring continues.</span><SoundButton/><button className="icon-button portrait-pause" data-tooltip="Portrait animation only. Monitoring continues." aria-pressed={paused} aria-label={paused ? 'Resume portrait animation' : 'Pause portrait animation'} aria-describedby="animation-pause-help" onClick={() => setPaused(!paused)}><Icon kind={paused ? 'play' : 'pause'}/></button><span className="transport-divider"/><button className="icon-button" aria-label="Portrait settings" aria-controls="scene-settings" aria-expanded={settings} onClick={() => { setSettings(!settings); setPanel(null); }}><Icon kind="sliders"/></button></div></footer>
    {settings && <PortraitSettings count={particleCount} onCount={v => setParticleCount(normalizeParticleCount(v))} quality={quality} onQuality={setQuality} reducedMotion={reducedMotion} onMotion={setReducedMotion} stats={stats} close={() => setSettings(false)}/>}
    {panel && <section ref={panelRef} tabIndex={-1} className="agent-panel ops-panel" id="ops-workspace" aria-label="Operations workspace"><div className="ops-panel-chrome"><div className="panel-heading"><h2>{panel === 'applications' ? 'My apps' : panel === 'actions' ? 'Review' : 'Activity'}</h2><button className="icon-button" aria-label="Close workspace" onClick={() => setPanel(null)}><Icon kind="close"/></button></div><nav className="ops-tabs" data-active={panel} aria-label="Workspace sections">{(['applications', 'investigations', 'actions'] as const).map(tab => <button key={tab} className={panel === tab ? 'selected' : ''} aria-pressed={panel === tab} onClick={() => setPanel(tab)}>{tabs[tab]}</button>)}</nav></div>
      {(ops.error || importError) && <p ref={errorMessage} tabIndex={-1} className="ops-error" role="alert">{ops.error || importError}</p>}
      {notice && <p className="connection-notice" role="status">{notice}</p>}
      {panel === 'applications' && <>
        {!ops.state && <div className="ops-empty"><Mark kind="pulse"/><h3>Waiting for AURA</h3><p>Start AURA to see your connected apps.</p></div>}
        {ops.state && targets.length === 0 && !enrolling && <div className="ops-empty first-connection"><Mark kind="file"/><h3>Connect your first app</h3><p>See its health and check problems here.</p></div>}
        {ops.state && !ops.state.docker.available && <div className="ops-empty"><h3>Docker is not ready</h3><p>Open Docker Desktop and wait for it to finish starting. AURA will reconnect automatically.</p></div>}
        {targets.map(t => <article className={`ops-target ${target?.target_id === t.target_id ? 'selected' : ''}`} key={t.target_id}><button className="ops-target-title" aria-pressed={target?.target_id === t.target_id} onClick={() => { setSelected(t.target_id); setExpanded(null); setAsking(false); setQuestion(''); }}><span>{t.name}</span><small>{observationLabel(t, ops.connection)}</small></button>{t.reconciliation_required && <button className="reconciliation-link" onClick={() => { setSelected(t.target_id); setPanel('actions'); }}>Changes on hold · Review app state</button>}{t.observation?.error && <p className="ops-error">{t.observation.error.code === 'docker_unavailable' ? 'Open Docker Desktop to resume watching.' : 'Could not reach this app. Check its container and address.'}</p>}<details><summary>Settings</summary>{t.observation?.error && <p className="ops-error">{t.observation.error.message}</p>}<p>{t.container_name} · {t.docker_context}</p><p>Memory: {size(t.observation?.resources?.memory_bytes)} of {size(t.observation?.resources?.memory_limit_bytes)}</p><TargetPolicy target={t} pending={ops.pending || ops.connection !== 'connected'} command={ops.command}/><div className="ops-inline-actions"><button disabled={ops.pending || t.observation?.healthy !== true} onClick={() => void ops.command(`targets/${t.target_id}/snapshot`)}>Save working settings</button><button disabled={ops.pending} onClick={() => void ops.command(`targets/${t.target_id}/${t.monitoring ? 'disconnect' : 'reconnect'}`)}>{t.monitoring ? 'Pause watching' : 'Resume watching'}</button></div><ConnectionSettings target={t} pending={ops.pending} connected={ops.connection === 'connected'} busy={!!ops.state?.investigations.some(r => r.target_id === t.target_id && ongoing.has(r.phase)) || pendingApprovals.some(p => p.target_id === t.target_id && !ops.state?.investigations.some(r => r.investigation_id === p.investigation_id && r.phase !== 'awaiting_approval'))} command={ops.command} onNotice={text => { setNotice(text); if (text.startsWith('Connection removed')) setAllHistory(true); }}/></details></article>)}
        {targets.length < 3 && <><button className="ops-add" aria-expanded={enrolling} aria-controls="connection-form" disabled={!ops.state || ops.connection !== 'connected'} onClick={() => setEnrolling(!enrolling)}>{enrolling ? 'Close connection form' : 'Connect an app'}</button>{enrolling && <form className="ops-form" id="connection-form" onSubmit={e => { e.preventDefault(); void enroll(); }}><h3>Connect an app</h3><label>App name<input ref={connectionName} name="appName" autoComplete="off" maxLength={80} required value={name} onChange={e => setName(e.target.value)} placeholder="My application"/></label><label>Docker container<input name="container" autoComplete="off" spellCheck={false} required value={container} onChange={e => setContainer(e.target.value)} placeholder="my-api" aria-describedby="container-help"/></label><p className="ops-small" id="container-help">Use the name shown in Docker Desktop → Containers.</p><label>App check address<input name="healthUrl" autoComplete="off" spellCheck={false} inputMode="url" placeholder="http://localhost:8080/health" required type="url" aria-describedby="address-help" value={url} onChange={e => setUrl(e.target.value)}/></label><p className="ops-small" id="address-help">An address that responds when the app is working, such as its health page.</p><details><summary>Advanced</summary><label>Docker context<input name="dockerContext" autoComplete="off" spellCheck={false} required value={context} onChange={e => setContext(e.target.value)}/></label><p className="ops-small">Docker Desktop on Windows often uses desktop-linux. Ask your app’s maintainer if unsure.</p></details><p className="ops-small">Connecting starts free health monitoring. AI checks and changes are off until you choose them.</p><button className="start-button" disabled={ops.pending}>{ops.pending ? 'Connecting…' : 'Connect'}</button></form>}</>}<input ref={fileInput} type="file" accept="application/json,.json" hidden onChange={e => void importTarget(e.target.files?.[0])}/><button className="text-button" disabled={!ops.state || ops.connection !== 'connected'} onClick={() => fileInput.current?.click()}>Import connection file</button>
        {targets.length >= 3 && <p className="ops-small">All three connections are in use. Pause and remove one to make room. Your app keeps running.</p>}
      </>}
      {panel === 'investigations' && <>
        <label className="history-filter">Show checks<select value={allHistory ? 'all' : 'current'} onChange={e => setAllHistory(e.target.value === 'all')}><option value="current">{target?.name ?? 'Current app'}</option><option value="all">All apps, including removed</option></select></label>
        {historyRuns.length === 0 && !target && <div className="ops-empty"><Mark kind="search"/><h3>No checks yet</h3><p>{target ? 'Choose Check app when something needs attention.' : 'Connect an app in My apps first.'}</p></div>}
        {historyRuns.map(run => <article className="ops-investigation" key={run.investigation_id}><button className="ops-run-title" aria-expanded={expanded === run.investigation_id || (expanded === null && run === current)} onClick={() => setExpanded(expanded === run.investigation_id || (expanded === null && run === current) ? '' : run.investigation_id)}><span>{allHistory || expanded === run.investigation_id || (expanded === null && run === current) ? targets.find(t => t.target_id === run.target_id)?.name ?? `Removed app · ${run.target_id}` : resultLabel(run)}</span><small>{new Date(run.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} · {new Date(run.created_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })} <span aria-hidden="true">⌄</span></small></button>{(expanded === run.investigation_id || (expanded === null && run === current)) && runDetails(run)}</article>)}
        {!allHistory && target && !busy && !fixReviewAvailable(current) && <form ref={checkForm} tabIndex={-1} className="ops-question ops-check-form" onSubmit={e => { e.preventDefault(); void investigate(); }}>
          <h3>Check {target.name}</h3>
          <details className="ops-ask" open={asking} onToggle={e => setAsking(e.currentTarget.open)}><summary>Add context <span className="optional-note">optional</span></summary>
            <label htmlFor="ops-question">What did you notice?</label><textarea ref={questionInput} id="ops-question" maxLength={1000} rows={2} value={question} onChange={e => setQuestion(e.target.value)} placeholder="For example: large files won’t download"/>
            <p className="ops-small">Included with the app’s logs and measurements.</p>
          </details>
          {blockedCheck && <p className="ops-error" role="status">{blockedCheck}</p>}
          <div className="ops-check-submit"><button className="start-button" disabled={ops.pending || busy || !!blockedCheck}>{ops.pending ? 'Starting…' : busy ? 'Check in progress' : 'Check app'}</button><span>Uses Baseten credits</span></div>
        </form>}
      </>}
      {panel === 'actions' && <>{blockedTargets.length > 0 && targets.length > 1 && <label className="reconciliation-select">App to review<select value={target?.target_id ?? ''} onChange={e => setSelected(e.target.value)}>{targets.map(t => <option key={t.target_id} value={t.target_id}>{t.name}{t.reconciliation_required ? ' — needs review' : ''}</option>)}</select></label>}{target && <Reconciliation key={target.target_id} target={target} connected={ops.connection === 'connected'} pending={ops.pending} command={ops.command}/>} {pendingApprovals.length === 0 && blockedTargets.length === 0 && <div className="ops-empty"><h3>Nothing to approve</h3><p>AURA will ask here before making a change that needs your permission.</p></div>}{pendingApprovals.map(proposal => <article className="ops-proposal" key={proposal.proposal_id}><h3>{actionName(proposal.action)}</h3><p>{targets.find(t => t.target_id === proposal.target_id)?.name ?? proposal.target_id}</p><p>{actionHelp(proposal.action)}</p><details className="ops-technical"><summary>Why this change?</summary><p>{proposal.reason}</p><p>Permission applies only to this change and app version.</p></details>{proposal.action === 'deploy_verified_patch' && <>{proposalChanges(proposal)}<p className="ops-small">If checks fail and it is safe, AURA will try the saved version once. Database structure stays unchanged.</p><details className="ops-evidence"><summary>Version details</summary><pre>{JSON.stringify({ patch: proposal.patch_id, base_image: proposal.base_image_id, candidate_image: proposal.candidate_image_id, changes: proposal.edited_hashes, server_launch: proposal.deployment_launch, approval_hash: proposal.proposal_hash }, null, 2)}</pre></details></>}<div className="ops-inline-actions"><button className="ops-approve" disabled={ops.pending || targets.some(t => t.target_id === proposal.target_id && t.reconciliation_required)} onClick={() => void decide(proposal, true)}>Allow this change</button><button disabled={ops.pending} onClick={() => void decide(proposal, false)}>Decline change</button></div></article>)}</>}
    </section>}
  </main>;
}
