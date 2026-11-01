export type Phase = 'idle' | 'fault_injected' | 'diagnosing' | 'remediating' | 'verifying' | 'resolved' | 'error' | 'cancelled';
export type Agent = { agent_id: string; status: 'idle' | 'running' | 'completed' | 'failed'; summary?: string | null; findings?: string[]; reasoning?: string | null; reasoning_truncated?: boolean; usage?: { total_tokens?: number | null } | null; duration_ms?: number | null };
export type Telemetry = { rss_bytes: number | null; baseline_rss_bytes: number | null; retained_cache_bytes: number | null; completed_jobs: number | null; failed_jobs: number | null; rss_fresh: boolean; status_fresh: boolean; sample_timestamp?: string | null; generation?: number | null };
export type Verification = { verified: boolean; duration_seconds: number; retained_cache_bytes?: number | null; rss_bytes?: number | null; generation?: number | null };
export type Snapshot = { seq: number; server_instance_id: string; run_id: string | null; phase: Phase; telemetry: Telemetry; agents: Agent[]; latest_verification: Verification | null; error: { code: string; message: string } | null; proposal?: { action: string; reason: string } | null; simulated_diagnosis: boolean };
export type Envelope = { seq: number; server_instance_id: string; run_id: string | null; type: string; timestamp: string; payload: Record<string, unknown> };
export type LogEntry = { seq: number; timestamp: string; agent: string; kind: string; message: string };
export type Runtime = { state: Snapshot | null; seq: number; server: string | null; log: LogEntry[]; samples: { time: string; rss: number | null; cache: number | null }[] };
export const emptyRuntime = (): Runtime => ({ state: null, seq: -1, server: null, log: [], samples: [] });
export const activePhase = (phase?: Phase) => !!phase && !['idle', 'resolved', 'error', 'cancelled'].includes(phase);
export const sceneForState = (state: Snapshot | null): 'streams' | 'portrait' => {
  if (!state || state.phase === 'idle') return 'portrait';
  if (state.phase === 'resolved' && state.latest_verification?.verified === true && !state.simulated_diagnosis) return 'portrait';
  return 'streams';
};

function appendSample(runtime: Runtime, telemetry: Telemetry): Runtime['samples'] {
  const time = telemetry.sample_timestamp;
  if (!time || runtime.samples.at(-1)?.time === time) return runtime.samples;
  return [...runtime.samples, { time, rss: telemetry.rss_fresh ? telemetry.rss_bytes : null,
    cache: telemetry.status_fresh ? telemetry.retained_cache_bytes : null }].slice(-120);
}

export function applySnapshot(runtime: Runtime, snapshot: Snapshot, seq = snapshot.seq): Runtime {
  if (!Number.isSafeInteger(seq) || typeof snapshot.server_instance_id !== 'string') return runtime;
  if (runtime.server === snapshot.server_instance_id && seq < runtime.seq) return runtime;
  const sameRun = runtime.server === snapshot.server_instance_id && runtime.state?.run_id === snapshot.run_id;
  const base = sameRun ? runtime : emptyRuntime();
  return { ...base, state: snapshot, seq, server: snapshot.server_instance_id, samples: appendSample(base, snapshot.telemetry) };
}

export function applyEvent(runtime: Runtime, event: Envelope): Runtime {
  if (!Number.isSafeInteger(event.seq)) return runtime;
  if (event.type === 'snapshot') return applySnapshot(runtime, event.payload as unknown as Snapshot, event.seq);
  if (!runtime.state || runtime.server !== event.server_instance_id || event.seq <= runtime.seq) return runtime;
  let next = { ...runtime, seq: event.seq }, state = { ...runtime.state };
  const p = event.payload;
  if (event.run_id !== state.run_id) {
    if (event.type === 'phase_changed' && p.to === 'fault_injected') {
      state = { ...state, run_id: event.run_id, phase: 'fault_injected', agents: state.agents.map(a => ({ agent_id: a.agent_id, status: 'idle' })), latest_verification: null, proposal: null, error: null };
      next = { ...next, log: [], samples: runtime.samples.slice(-5) };
    } else return next;
  }
  if (event.type === 'telemetry') {
    state.telemetry = p as unknown as Telemetry;
    if (!['resolved', 'error', 'cancelled'].includes(state.phase)) next.samples = appendSample(next, state.telemetry);
  }
  if (event.type === 'phase_changed') state.phase = p.to as Phase;
  if (event.type === 'verification') state.latest_verification = p as unknown as Verification;
  if (event.type === 'error') state.error = { code: String(p.code), message: String(p.message) };
  if (event.type === 'action_proposed') state.proposal = { action: String(p.action), reason: String(p.reason) };
  if (event.type === 'agent_started' || event.type === 'agent_finished') {
    state.agents = state.agents.map(a => a.agent_id === p.agent_id ? { ...a,
      status: event.type === 'agent_started' ? 'running' : p.status as Agent['status'], summary: p.summary as string | undefined ?? a.summary } : a);
  }
  if (event.type === 'agent_message') {
    next.log = [...next.log, { seq: event.seq, timestamp: event.timestamp, agent: String(p.agent_id), kind: String(p.kind), message: String(p.message).slice(0, 2000) }].slice(-100);
  }
  return { ...next, state };
}
