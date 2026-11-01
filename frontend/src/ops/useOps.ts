import { useCallback, useEffect, useRef, useState } from 'react';

export type Observation = {
  timestamp: string; healthy: boolean | null; error?: { code: string; message: string };
  resources?: { memory_bytes: number | null; memory_limit_bytes: number | null; cpu_percent: number | null };
  identity?: { container_id: string; image_id: string; started_at: string };
  probes?: { url: string; ok: boolean; status_code: number | null; duration_ms: number }[];
};
export type ConfigurationSection = { configured: boolean; missing: string[] };
export type Configuration = Record<'monitoring' | 'restart' | 'configuration_recovery' | 'code_fix' | 'deployment', ConfigurationSection>;
export type Probe = { url: string; expected_status?: number; credential_env?: string | null; auth_scheme?: string; timeout_seconds?: number };
export type Target = { target_id: string; name: string; container_name: string; docker_context: string; monitoring: boolean;
  connection_revision?: number; configuration?: Configuration; probes?: Probe[];
  source_revision?: string | null; source_manifest_sha256?: string | null; source_image_id?: string | null;
  compose_file?: string | null; compose_project?: string | null; compose_service?: string | null;
  deployment_root?: string | null; deployment_launch?: unknown; recovery_checks?: unknown[];
  reconciliation_required?: boolean; unresolved_unknown_ids?: string[];
  policy: { auto_investigate: boolean; automatic_actions: string[]; restart_cooldown_seconds: number };
  source_dir?: string | null; patch_profile?: unknown; observation?: Observation;
  verified_deployment?: { proposal_id: string; timestamp: string; image_id: string; patch_id: string } };
export type Investigation = { investigation_id: string; target_id: string; question: string; trigger?: 'monitor' | 'manual'; phase: string; created_at: string; connection_revision?: number;
  error?: { code: string; message: string } | null;
  baseline_promoted?: boolean; baseline_error?: { code: string; message: string } | null;
  diagnosis?: { summary: string; reason: string; action: string; needs_source_analysis: boolean; findings: { hypothesis: string; confidence: string; evidence_refs: string[] }[] };
  verification?: { verified: boolean; kind: string; root_cause_fixed: boolean; duration_seconds?: number; reason?: string;
    functional?: { verified: boolean; checks?: { ok: boolean; bytes_received: number; expected_bytes: number; sha256?: string; expected_sha256?: string }[]; oom_kill_delta?: number } };
  evidence?: Record<string, unknown>; calls?: { agent_id: string; duration_ms: number; usage?: { total_tokens: number } }[];
  patch?: { status: string; artifact?: { artifact_id: string }; attempts?: { status: string; tests?: { stage: string; gate_passed?: boolean; passed?: boolean; exit_code: number }[] }[]; [key: string]: unknown };
};
export type Proposal = { proposal_id: string; proposal_hash: string; investigation_id: string; target_id: string; action: string; reason?: string; status: string; [key: string]: unknown };
export type OpsSnapshot = { seq: number; provider_ready: boolean; docker: { available: boolean; message?: string }; targets: Target[]; investigations: Investigation[]; proposals: Proposal[]; active_id: string | null };
export const phaseLabel: Record<string, string> = { queued: 'Waiting', investigating: 'Looking for the cause', awaiting_approval: 'Your permission needed', executing: 'Making the change', verifying: 'Checking the result', mitigated: 'App responding again', fix_proposed: 'Fix ready to review', verified_fixed: 'Fix checked', needs_human: 'Needs your help', cancelled: 'Check stopped', preparing_patch: 'Testing a fix', preparing_deployment: 'Getting the fix ready' };
export const ongoing = new Set(['queued', 'investigating', 'awaiting_approval', 'executing', 'verifying', 'preparing_patch', 'preparing_deployment']);

export function useOps() {
  const [state, setState] = useState<OpsSnapshot | null>(null);
  const [connection, setConnection] = useState<'connecting' | 'connected' | 'disconnected'>('connecting');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const commandBusy = useRef(false);
  const alive = useRef(true), seq = useRef(0);
  const refresh = useCallback(async () => {
    const response = await fetch('/api/v2/status');
    if (!response.ok) throw new Error('Operations service is unavailable. Start the updated AURA backend.');
    const snapshot: OpsSnapshot = await response.json();
    if (alive.current && snapshot.seq >= seq.current) { seq.current = snapshot.seq; setState(snapshot); }
  }, []);
  useEffect(() => {
    alive.current = true;
    let socket: WebSocket | null = null, timer = 0, refreshTimer = 0;
    let disposed = false;
    const update = () => { clearTimeout(refreshTimer); refreshTimer = window.setTimeout(() => { void refresh().catch(() => {}); }, 120); };
    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/ops`);
      socket.onopen = () => { if (!disposed) { setConnection('connected'); setError(''); } };
      socket.onmessage = event => {
        if (disposed) return;
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'snapshot') { seq.current = data.seq; setState(data.payload); }
          else if (data.seq > seq.current) update();
        } catch { update(); }
      };
      socket.onclose = () => { if (!disposed) { setConnection('disconnected'); timer = window.setTimeout(connect, 2000); } };
    };
    void refresh().catch(e => { if (!disposed) setError(e.message); });
    connect();
    const poll = window.setInterval(() => { void refresh().catch(() => {}); }, 10000);
    return () => { disposed = true; alive.current = false; clearTimeout(timer); clearTimeout(refreshTimer); clearInterval(poll); socket?.close(); };
  }, [refresh]);
  const command = useCallback(async (path: string, body?: unknown, method: 'POST' | 'PUT' | 'DELETE' = 'POST') => {
    if (commandBusy.current) return null;
    commandBusy.current = true;
    setPending(true); setError('');
    try {
      const response = await fetch(`/api/v2/${path}`, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 409 || response.status === 404) {
          // Refresh stale forms, but never replay a rejected mutation.
          try { await refresh(); } catch { /* Keep the original action error. */ }
        }
        const explanations: Record<string, string> = {
          target_revision_changed: 'These settings changed. Close the editor and reopen it to use the latest version.',
          target_configuration_changed: 'This check used earlier settings. Start a new check for the current connection.',
          target_id_retired: 'This connection ID belongs to a removed app. Use a new ID in the connection file.',
          target_must_be_disconnected: 'Pause watching this app before changing its connection.',
          target_reconciliation_required: 'Review the previous change in Review before editing this connection.',
          target_already_enrolled: 'This app is already connected. Open its settings in My apps.',
          restart_cooldown: 'This app was changed recently. Wait for its restart waiting period to end, then try again.',
        };
        throw new Error(explanations[data.error?.code] ?? data.error?.message ?? (Array.isArray(data.detail) ? data.detail.map((d: {msg: string}) => d.msg).join('; ') : 'Request could not be completed.'));
      }
      try { await refresh(); } catch { setError('Request accepted. Reconnecting to update the result.'); }
      return data;
    } catch (e) { setError(e instanceof Error ? e.message : 'Request failed.'); return null; }
    finally { commandBusy.current = false; setPending(false); }
  }, [refresh]);
  return { state, connection, error, pending, command };
}
