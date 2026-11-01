import { useEffect, useState } from 'react';

export default function PatchDiff({ artifactId }: { artifactId: string }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(''), [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setError(''); setText('');
    void fetch(`/api/v2/artifacts/${encodeURIComponent(artifactId)}`, { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error('The reviewed artifact is unavailable or its integrity check failed.');
        const content = await response.text();
        if (!controller.signal.aborted) setText(content.length > 150000 ? `${content.slice(0, 150000)}\n\n[Preview truncated. Download the complete diff below.]` : content);
      }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [artifactId, open]);
  return <details className="ops-evidence" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
    <summary>Review proposed changes</summary>
    {error ? <p className="ops-error">{error}</p> : <pre className="ops-diff">{text || 'Loading verified artifact…'}</pre>}
    <a href={`/api/v2/artifacts/${encodeURIComponent(artifactId)}`}>Download complete diff</a>
  </details>;
}
