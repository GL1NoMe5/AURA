import { useCallback, useEffect, useRef, useState } from 'react';
import { activePhase, applyEvent, applySnapshot, emptyRuntime } from './model';
import type { Envelope, Runtime, Snapshot } from './model';

type Health = { orchestrator_ready: boolean; provider_configured: boolean; worker_running: boolean };
type Connection = 'connecting' | 'connected' | 'reconnecting';

async function jsonRequest(path: string, init?: RequestInit) {
  const response = await fetch(path, { ...init, signal: init?.signal ?? AbortSignal.timeout(15000) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message ?? `Request failed (${response.status}).`);
  return result;
}

export function useRuntime(enabled: boolean) {
  const [runtime, setRuntime] = useState<Runtime>(emptyRuntime);
  const current = useRef(runtime);
  const [connection, setConnection] = useState<Connection>('connecting');
  const [health, setHealth] = useState<Health | null>(null);
  const [pending, setPending] = useState<'start' | 'reset' | null>(null);
  const [error, setError] = useState('');
  const refresh = useRef<() => void>(() => {});
  const version = useRef(0);
  const mutate = useCallback((fn: (r: Runtime) => Runtime) => {
    current.current = fn(current.current); setRuntime(current.current);
  }, []);

  useEffect(() => {
    const token = ++version.current;
    mutate(emptyRuntime); setHealth(null); setError('');
    if (!enabled) return;
    let stopped = false, socket: WebSocket | null = null, timer: ReturnType<typeof setTimeout> | undefined;
    let lastEvent = Date.now(), tries = 0, resyncing = false, buffered: Envelope[] = [];
    const abort = new AbortController();
    const valid = () => !stopped && version.current === token;
    const readHealth = () => void jsonRequest('/health', { signal: abort.signal }).then(h => { if (valid()) setHealth(h); }).catch(() => { if (valid()) setHealth(null); });
    const resync = async () => {
      if (resyncing || !valid()) return;
      resyncing = true; buffered = [];
      try {
        const snap: Snapshot = await jsonRequest('/api/state', { signal: AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]) });
        if (!valid()) return;
        mutate(r => buffered.reduce(applyEvent, applySnapshot(r, snap)));
      } catch {
        if (valid()) socket?.close();
      } finally { resyncing = false; buffered = []; }
    };
    refresh.current = () => { readHealth(); void resync(); };
    function connect() {
      if (!valid()) return;
      lastEvent = Date.now();
      setConnection(tries ? 'reconnecting' : 'connecting');
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/telemetry`);
      socket = ws;
      ws.onmessage = message => {
        if (!valid() || socket !== ws) return;
        try {
          const event = JSON.parse(message.data) as Envelope;
          if (!event.payload || !Number.isSafeInteger(event.seq) || typeof event.server_instance_id !== 'string') throw new Error('invalid event');
          lastEvent = Date.now(); tries = 0;
          if (event.type === 'snapshot') {
            mutate(r => applyEvent(r, event)); setConnection('connected'); readHealth();
          } else if (resyncing) {
            buffered.push(event); if (buffered.length > 200) ws.close();
          } else if (current.current.server !== event.server_instance_id || event.seq > current.current.seq + 1) {
            void resync(); buffered.push(event);
          } else {
            mutate(r => applyEvent(r, event));
            if (event.type === 'phase_changed' && ['resolved', 'error', 'cancelled'].includes(String(event.payload.to))) void resync();
          }
        } catch { ws.close(); }
      };
      ws.onerror = () => ws.close();
      ws.onclose = () => {
        if (!valid() || socket !== ws) return;
        setConnection('reconnecting'); setHealth(null);
        timer = setTimeout(connect, Math.min(5000, 500 * 2 ** Math.min(tries++, 4)));
      };
    }
    connect();
    const heartbeat = setInterval(() => { if (valid() && Date.now() - lastEvent > 8000) { setConnection('reconnecting'); socket?.close(); } }, 2000);
    return () => { stopped = true; refresh.current = () => {}; abort.abort(); clearTimeout(timer); clearInterval(heartbeat); socket?.close(); };
  }, [enabled, mutate]);

  const command = async (kind: 'start' | 'reset') => {
    if (pending || !enabled) return;
    const token = version.current;
    setPending(kind); setError('');
    try {
      await jsonRequest(kind === 'start' ? '/api/incidents' : '/api/reset', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: kind === 'start' ? JSON.stringify({ scenario: 'cache_retention' }) : undefined,
        signal: AbortSignal.timeout(45000),
      });
      if (version.current === token) refresh.current();
    } catch (err) { if (version.current === token) { setError(err instanceof Error ? err.message : 'Request failed.'); refresh.current(); } }
    finally { if (version.current === token) setPending(null); }
  };
  const canStart = enabled && connection === 'connected' && health?.orchestrator_ready && !!runtime.state && !activePhase(runtime.state.phase) && runtime.state.phase !== 'error' && !pending;
  return { ...runtime, connection, health, pending, error, canStart, start: () => command('start'), reset: () => command('reset') };
}
