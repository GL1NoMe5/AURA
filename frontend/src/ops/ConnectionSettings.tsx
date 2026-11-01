import { useRef, useState } from 'react';
import type { Configuration, Target } from './useOps';
import { editableProfile, importProfile, profileUpdate, setupNames } from './connectionProfile';

type Command = (path: string, body?: unknown, method?: 'POST' | 'PUT' | 'DELETE') => Promise<unknown>;
type Props = { target: Target; pending: boolean; connected: boolean; busy: boolean; command: Command; onNotice: (text: string) => void };
const capabilities: [keyof Configuration, string][] = [
  ['monitoring', 'Watch health'], ['restart', 'Restart the app'],
  ['configuration_recovery', 'Restore settings'], ['code_fix', 'Test code fixes'], ['deployment', 'Install a tested fix'],
];

export default function ConnectionSettings({ target, pending, connected, busy, command, onNotice }: Props) {
  const [editor, setEditor] = useState<{ target: Target; draft: Record<string, unknown> } | null>(null);
  const [name, setName] = useState(''), [address, setAddress] = useState('');
  const [remove, setRemove] = useState<Target | null>(null), [error, setError] = useState(''), [loaded, setLoaded] = useState('');
  const fileInput = useRef<HTMLInputElement>(null), editButton = useRef<HTMLButtonElement>(null);
  const blocked = !connected ? 'Reconnect to AURA before editing.' : target.reconciliation_required ? 'Review the previous change in Review first.' : busy ? 'Finish or stop the current check first.' : target.monitoring ? 'Pause watching to edit or remove this connection.' : '';
  const stale = !!editor && (target.connection_revision ?? 0) !== (editor.target.connection_revision ?? 0);
  const removeStale = !!remove && target.connection_revision !== remove.connection_revision;
  const revisionAvailable = typeof target.connection_revision === 'number';
  const disabled = pending || !!blocked || !revisionAvailable;
  const closeEditor = () => { setEditor(null); setError(''); setLoaded(''); requestAnimationFrame(() => editButton.current?.focus()); };

  function begin() {
    setEditor({ target: structuredClone(target), draft: editableProfile(target) });
    setName(target.name); setAddress(target.probes?.[0]?.url ?? '');
    setError(''); setLoaded(''); setRemove(null); onNotice('');
  }
  async function loadFile(file?: File) {
    if (!file || !editor) return;
    try {
      if (file.size > 256 * 1024) throw new Error('Choose a connection file smaller than 256 KB.');
      const draft = importProfile(await file.text(), editor.target);
      setEditor({ ...editor, draft }); setName(String(draft.name ?? editor.target.name));
      setAddress((draft.probes as { url: string }[])[0].url);
      setLoaded('Setup loaded. Review it, then save.'); setError('');
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not read this connection file.'); }
    if (fileInput.current) fileInput.current.value = '';
  }
  function field(key: string, value: string) {
    if (!editor) return;
    const draft = { ...editor.draft, [key]: value.trim() ? value : null };
    if (key === 'source_dir' || key === 'source_revision') draft.source_manifest_sha256 = null;
    setEditor({ ...editor, draft });
  }
  async function save() {
    if (!editor || disabled || stale) return;
    setError('');
    try {
      const body = profileUpdate(editor.target, editor.draft, name, address);
      if (await command(`targets/${target.target_id}`, body, 'PUT')) {
        closeEditor(); onNotice('Connection saved. Resume watching when you’re ready. Automatic actions are off.');
      }
    } catch (e) { setError(e instanceof Error ? e.message : 'Check the connection details.'); }
  }
  async function forget() {
    if (disabled || !remove || removeStale) return;
    if (await command(`targets/${target.target_id}`, { expected_revision: remove.connection_revision }, 'DELETE')) {
      onNotice('Connection removed. Your app is still running. Its checks remain in Activity.');
    }
  }

  return <div className="connection-settings">
    <details className="connection-capabilities"><summary>What’s set up</summary>
      {target.configuration ? <ul>{capabilities.map(([key, label]) => {
        const item = target.configuration![key];
        return <li key={key}><div><span>{label}</span><small className={item.configured ? 'is-configured' : ''}>{item.configured ? 'Set up' : 'Needs setup'}</small></div>
          {!item.configured && <p>Add {item.missing.map(k => setupNames[k] ?? k.replaceAll('_', ' ')).join(', ')}.</p>}</li>;
      })}</ul> : <p>Restart AURA with the updated backend to see connection setup.</p>}
      <p className="ops-small">Setup describes available tools. Changes still follow your permissions; installing a fix always needs approval.</p>
    </details>
    {!editor && !remove && <>
      <div className="ops-inline-actions connection-actions">
        <button ref={editButton} disabled={disabled} onClick={begin}>Edit connection</button>
        <button className="remove-connection" disabled={disabled} onClick={() => { setRemove(structuredClone(target)); setError(''); onNotice(''); }}>Remove from AURA</button>
      </div>
      <p className="ops-small">{!revisionAvailable ? 'Restart AURA to enable connection editing.' : blocked || 'Your app keeps running while its connection is edited.'}</p>
    </>}
    {editor && <form className="ops-form connection-editor" aria-label={`Edit connection for ${target.name}`} onSubmit={e => { e.preventDefault(); void save(); }}>
      <h3>Edit connection</h3>
      <p className="connection-identity">{target.container_name}<span>{target.docker_context}</span></p>
      <p className="ops-small">To use a different container or Docker context, connect a new app.</p>
      <fieldset disabled={pending || !!blocked || stale}>
        <label>App name<input autoFocus maxLength={80} required autoComplete="off" value={name} onChange={e => setName(e.target.value)}/></label>
        <label>Check address<input type="url" required autoComplete="off" spellCheck={false} value={address} onChange={e => setAddress(e.target.value)}/></label>
        {(editor.target.probes?.length ?? 0) > 1 && <p className="ops-small">Other check addresses and their credentials are kept.</p>}
        <details className="repair-setup"><summary>Repair setup <span>optional</span></summary>
          <p>Use the deployment and source details from the person who runs this app. Test and install profiles can be loaded from a connection file.</p>
          <p className="connection-identity">Connection ID: {target.target_id}</p>
          <button className="connection-load" type="button" onClick={() => fileInput.current?.click()}>Load setup file</button>
          <input ref={fileInput} type="file" accept="application/json,.json" hidden onChange={e => void loadFile(e.target.files?.[0])}/>
          {loaded && <p role="status">{loaded}</p>}
          <div className="connection-field-group"><h4>Deployment settings</h4>
            <label>Compose file<input autoComplete="off" spellCheck={false} value={String(editor.draft.compose_file ?? '')} onChange={e => field('compose_file', e.target.value)} placeholder="D:\\apps\\my-app\\compose.yaml"/></label>
            <label>Compose project<input autoComplete="off" spellCheck={false} value={String(editor.draft.compose_project ?? '')} onChange={e => field('compose_project', e.target.value)}/></label>
            <label>Service name<input autoComplete="off" spellCheck={false} value={String(editor.draft.compose_service ?? '')} onChange={e => field('compose_service', e.target.value)}/></label>
          </div>
          <div className="connection-field-group"><h4>Application code</h4>
            <label>Source folder<input autoComplete="off" spellCheck={false} value={String(editor.draft.source_dir ?? '')} onChange={e => field('source_dir', e.target.value)}/></label>
            <label>Source commit<input autoComplete="off" spellCheck={false} maxLength={40} value={String(editor.draft.source_revision ?? '')} onChange={e => field('source_revision', e.target.value)} placeholder="Full 40-character commit"/></label>
            <label>Matching Docker image<input autoComplete="off" spellCheck={false} value={String(editor.draft.source_image_id ?? '')} onChange={e => field('source_image_id', e.target.value)} placeholder="sha256:…"/></label>
          </div>
          <details className="ops-evidence"><summary>Test and install profile</summary><pre>{JSON.stringify({ patch_profile: editor.draft.patch_profile ?? null, deployment_root: editor.draft.deployment_root ?? null, deployment_launch: editor.draft.deployment_launch ?? null, recovery_checks: editor.draft.recovery_checks ?? [] }, null, 2)}</pre></details>
        </details>
      </fieldset>
      <p className="connection-effect">Saving changed settings starts a fresh connection review. Past results stay in Activity; saved working settings and automatic actions need to be set again.</p>
      {(stale || blocked || error) && <p className="ops-error" role="alert">{stale ? 'Settings changed elsewhere. Close this editor and reopen it before saving.' : error || blocked}</p>}
      <div className="ops-inline-actions"><button className="ops-approve" disabled={disabled || stale} type="submit">{pending ? 'Saving…' : 'Save connection'}</button><button type="button" disabled={pending} onClick={closeEditor}>Cancel</button></div>
    </form>}
    {remove && <div className="connection-remove" role="group" aria-label={`Remove ${target.name} from AURA`}>
      <h3>Remove this connection?</h3><p>{remove.name} keeps running. Previous checks and results stay in Activity.</p>
      {(blocked || removeStale) && <p className="ops-error" role="alert">{removeStale ? 'Settings changed elsewhere. Keep this connection and review its updated settings first.' : blocked}</p>}
      <div className="ops-inline-actions"><button className="remove-connection" disabled={disabled || removeStale} onClick={() => void forget()}>Remove connection</button><button disabled={pending} onClick={() => { setRemove(null); requestAnimationFrame(() => editButton.current?.focus()); }}>Keep connection</button></div>
    </div>}
  </div>;
}
