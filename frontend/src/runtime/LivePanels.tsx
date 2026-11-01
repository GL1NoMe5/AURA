import type { Runtime, Telemetry } from './model';

export const mib = (bytes: number | null | undefined) => bytes == null ? '—' : `${(bytes / 1048576).toFixed(1)} MiB`;
export const phaseLabels = { idle: 'Observing', fault_injected: 'Cache incident', diagnosing: 'Diagnosing', remediating: 'Restoring policy', verifying: 'Verifying recovery', resolved: 'Recovery verified', error: 'Run stopped', cancelled: 'Run cancelled' };

export function MemoryTrace({ samples }: { samples: Runtime['samples'] }) {
  const times = samples.map(s => Date.parse(s.time)), begin = times[0] ?? 0, end = times.at(-1) ?? begin;
  const maximum = Math.max(32 * 1048576, ...samples.flatMap(s => [s.rss ?? 0, s.cache ?? 0])) * 1.1;
  const path = (key: 'rss' | 'cache') => {
    let drawing = false;
    return samples.map((sample, i) => {
      const value = sample[key];
      if (value == null) { drawing = false; return ''; }
      const part = `${drawing ? 'L' : 'M'}${(4 + (times[i] - begin) / Math.max(1, end - begin) * 242).toFixed(1)},${(61 - value / maximum * 53).toFixed(1)}`;
      drawing = true; return part;
    }).join(' ');
  };
  return <svg className="memory-trace" viewBox="0 0 250 74" role="img" aria-label="Measured process memory and retained cache over time">
    <path d="M4 61H246M4 34H246M4 8H246" stroke="currentColor" opacity=".12" fill="none"/>
    <path d={path('rss')} stroke="#9bdcdb" strokeWidth="1.4" fill="none"/><path d={path('cache')} stroke="#ddb987" strokeWidth="1.4" fill="none"/>
  </svg>;
}

export function LiveMetrics({ telemetry, samples, connected }: { telemetry?: Telemetry; samples: Runtime['samples']; connected: boolean }) {
  return <section className="live-metrics" aria-label="Measured service telemetry">
    <div><span>Process memory</span><strong>{mib(connected && telemetry?.rss_fresh ? telemetry.rss_bytes : null)}</strong></div>
    <div><span>Retained cache</span><strong className="cache-value">{mib(connected && telemetry?.status_fresh ? telemetry.retained_cache_bytes : null)}</strong></div>
    <MemoryTrace samples={samples}/>
    <p>{connected && telemetry?.status_fresh ? `${telemetry.completed_jobs ?? '—'} completed / ${telemetry.failed_jobs ?? '—'} rejected` : 'Waiting for fresh measurements'}</p>
  </section>;
}

export function AgentDetails({ runtime, close }: { runtime: Runtime; close: () => void }) {
  const state = runtime.state;
  return <section className="agent-panel" aria-label="Agent activity">
    <div className="panel-heading"><h2>Agent activity</h2><button className="icon-button" aria-label="Close agent activity" onClick={close}>×</button></div>
    <p className="agent-model">Open 1026 {state?.simulated_diagnosis ? ' / Simulated diagnosis' : '/ Baseten'}</p>
    <div className="agent-list">{state?.agents.map(agent => <details key={agent.agent_id} className={`agent-row ${agent.status}`}>
      <summary><i/><span>{agent.agent_id === 'config' ? 'Configuration' : agent.agent_id === 'vision' ? 'Vision' : agent.agent_id[0].toUpperCase() + agent.agent_id.slice(1)}</span><small>{agent.status}</small></summary>
      <p>{agent.summary ?? 'No findings yet.'}</p>
      {!!agent.findings?.length && <ul>{agent.findings.map((finding, i) => <li key={i}>{finding}</li>)}</ul>}
      {(agent.duration_ms != null || agent.usage?.total_tokens != null) && <p className="agent-measurements">{agent.duration_ms == null ? '' : `${(agent.duration_ms / 1000).toFixed(2)} s`}{agent.usage?.total_tokens == null ? '' : ` / ${agent.usage.total_tokens} tokens`}</p>}
      {agent.reasoning && <details className="reasoning"><summary>Provider reasoning{agent.reasoning_truncated ? ' (excerpt)' : ''}</summary><p>{agent.reasoning}</p></details>}
    </details>)}</div>
    <details className="event-journal"><summary>Event journal <span>{runtime.log.length}</span></summary>
      {runtime.log.length === 0 && <p>No agent messages yet.</p>}
      {runtime.log.map(entry => <article key={entry.seq}><small>{new Date(entry.timestamp).toLocaleTimeString('en-GB', { hour12: false })} / {entry.agent} / {entry.kind === 'reasoning' ? 'Provider reasoning excerpt' : entry.kind}</small><p>{entry.message}</p></article>)}
    </details>
  </section>;
}
