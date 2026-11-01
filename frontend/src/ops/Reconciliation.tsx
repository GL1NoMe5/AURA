import { useEffect, useState } from 'react';
import type { Target } from './useOps';
import { Mark } from './CheckStory';

export const ATTESTATION = 'I have inspected the displayed target state and confirm the previous operation has ended.';
type Review = {
  review_id: string; review_hash: string; target_id: string; expires_at: string;
  container_name: string; docker_context: string; healthy: boolean;
  identity: { container_id: string; image_id: string; started_at: string };
  runtime: { running: boolean; status: string };
  config: { memory_limit_bytes: number; published_ports: Record<string, unknown>; server_port: number | null };
  probes: { url: string; ok: boolean; status_code: number }[];
  observations: { timestamp: string }[];
  unknowns: { proposal_id: string; action: string }[];
};
type Receipt = { result: string; remaining_gate: { reconciliation_required: boolean } };
type Props = { target: Target; connected: boolean; pending: boolean;
  command: (path: string, body?: unknown) => Promise<any> };

export default function Reconciliation({ target, connected, pending, command }: Props) {
  const [review, setReview] = useState<Review | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [checked, setChecked] = useState(false), [note, setNote] = useState('');
  const [working, setWorking] = useState<'collect' | 'confirm' | null>(null);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!review) return;
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [review]);
  useEffect(() => { if (!connected) { setReview(null); setChecked(false); setNote(''); } }, [connected]);
  const expired = !!review && Date.parse(review.expires_at) <= now;
  const changed = !!review && JSON.stringify(review.unknowns.map(u => u.proposal_id).sort()) !==
    JSON.stringify([...(target.unresolved_unknown_ids ?? [])].sort());
  const busy = pending || !!working;
  async function collect() {
    setWorking('collect'); setReview(null); setReceipt(null); setChecked(false); setNote('');
    try {
      const data = await command(`targets/${target.target_id}/reconciliations`);
      if (data) { setReview(data); setNow(Date.now()); }
    } finally { setWorking(null); }
  }
  async function confirm() {
    if (!review || !checked || !note.trim() || expired || changed || busy || !connected) return;
    setWorking('confirm');
    try {
      const data = await command(`targets/${target.target_id}/reconciliations/${review.review_id}/confirm`, {
        review_hash: review.review_hash, attestation: ATTESTATION, operator_note: note.trim(),
      });
      if (data) { setReceipt(data); setReview(null); setChecked(false); setNote(''); }
      else { setReview(null); setChecked(false); } // Recollect after uncertain/error responses; never silently retry.
    } finally { setWorking(null); }
  }
  if (!target.reconciliation_required && !review && !receipt && !working) return null;
  return <article className="ops-reconciliation" aria-label={`Review ${target.name} state`}>
    {receipt && <div className="reconciliation-receipt" role="status">
      <Mark kind="file"/><div><h3>Review saved</h3><p>{receipt.remaining_gate.reconciliation_required || target.reconciliation_required
        ? 'Another operation needs review before changes can resume.'
        : 'Automatic changes are off. Start a new check when you’re ready.'}</p><p>This records your review. It does not confirm a repair.</p></div>
    </div>}
    {(target.reconciliation_required || review || working) && <>
      <h3>Check before continuing</h3>
      <p>AURA lost track of an earlier change to {target.name}. Further changes are on hold.</p>
      {!review && <>
        <p className="ops-small">Read the app’s current state. Nothing will be changed.</p>
        <button className="start-button" disabled={busy || !connected} onClick={() => void collect()}>
          {working === 'collect' ? 'Reading app state…' : working === 'confirm' ? 'Checking your review…' : 'Read app state'}
        </button>
      </>}
      {review && <>
        <dl className="reconciliation-facts">
          <div><dt>Application</dt><dd>{target.name}</dd></div>
          <div><dt>Responding</dt><dd><i className={review.healthy ? 'reachable' : 'unreachable'}/>{review.healthy ? 'Yes' : 'Not as expected'}</dd></div>
          <div><dt>Process</dt><dd>{review.runtime.running ? 'Running' : 'Stopped'}</dd></div>
          <div><dt>Checked</dt><dd>{new Date(review.observations.at(-1)?.timestamp ?? '').toLocaleTimeString('en-GB')}</dd></div>
        </dl>
        <details className="ops-technical reconciliation-evidence"><summary>Measurements and version</summary>
          <p>{review.container_name} on {review.docker_context}</p>
          <p>Memory limit: {review.config.memory_limit_bytes ? `${Math.round(review.config.memory_limit_bytes / 1048576)} MiB` : 'No container limit'}</p>
          {review.probes.map(p => <p key={p.url}>{p.url}<br/>HTTP {p.status_code} · {p.ok ? 'Expected response' : 'Unexpected response'}</p>)}
          <p>Container <code>{review.identity.container_id}</code></p>
          <p>Image <code>{review.identity.image_id}</code></p>
          <p>Started {new Date(review.identity.started_at).toLocaleString('en-GB')}</p>
          <p>Published ports <code>{JSON.stringify(review.config.published_ports)}</code></p>
          <p>App port: {review.config.server_port ?? 'Not reported'}</p>
          {review.unknowns.map(u => <p key={u.proposal_id}>Earlier operation: {u.action.replaceAll('_', ' ')}<br/><code>{u.proposal_id}</code></p>)}
        </details>
        <p className="reconciliation-limit">These readings cannot tell whether the earlier command has ended. Confirm that separately before continuing.</p>
        <form className="reconciliation-form" onSubmit={e => { e.preventDefault(); void confirm(); }}>
          <label className="ops-check"><input type="checkbox" checked={checked} disabled={busy || expired || changed || !connected} onChange={e => setChecked(e.target.checked)}/>
            <span>I checked this app’s state and confirmed that the earlier operation has ended.</span></label>
          <label>What did you check?<textarea rows={2} maxLength={500} value={note} disabled={busy || expired || changed || !connected} onChange={e => setNote(e.target.value)} placeholder="Describe how you confirmed it ended. Don’t include passwords or keys."/></label>
          <p className="ops-small">Saving clears old approvals and turns automatic changes off. AURA will not repeat the earlier command or mark the problem fixed.</p>
          {(expired || changed) && <p className="ops-error" role="status">{expired ? 'This review expired.' : 'The operation list changed.'} Read the app state again.</p>}
          <div className="ops-inline-actions">
            <button className="ops-approve" disabled={!checked || !note.trim() || busy || expired || changed || !connected}>{working === 'confirm' ? 'Checking current state…' : 'Save my review'}</button>
            <button type="button" disabled={busy || !connected} onClick={() => void collect()}>Read again</button>
          </div>
        </form>
      </>}
    </>}
  </article>;
}
