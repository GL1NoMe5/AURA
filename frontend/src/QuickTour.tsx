import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import './tour.css';

type Step = { title: string; text: string; note?: string; anchor?: string; route?: string[]; outcomes?: boolean; symbol: 'portrait' | 'apps' | 'check' | 'activity' | 'settings' | 'return' };
const portrait: Step = {
  title: 'A little light, at your fingertips.',
  text: 'Move across the portrait to stir the particles. Hold to gather them. Release to let them go.',
  symbol: 'portrait',
};
const opsSteps: Step[] = [
  { title: 'Meet your app’s quiet helper.', text: 'AURA watches a connected app, investigates problems and checks whether its changes worked.', route: ['Watch', 'Find the cause', 'Check the result'], note: 'This tour explains the controls. It won’t start checks or change your app.', symbol: 'apps' },
  { title: 'Connect your app.', text: 'Open Docker, then add your app’s container name and check address. Ask the person who runs the app for these details, or import their connection file.', route: ['My apps', 'Connect an app'], note: 'A new connection starts with health and memory monitoring. Automatic AI checks are off.', anchor: 'apps', symbol: 'apps' },
  { title: 'Describe the problem.', text: 'Choose Check app to open the check form. Add what you noticed — for example, “Large files won’t download” — or leave it blank for a general check.', route: ['Activity', 'Add context'], note: 'The button inside the form starts the check and uses Baseten credits. Opening the form and basic monitoring are free.', anchor: 'check', symbol: 'check' },
  { title: 'Choose what can change.', text: 'Review explains a proposed change. You can allow it or decline it. AURA then checks the result of any change it carries out.', route: ['Activity', 'Review'], note: 'Automatic restarts need your permission in Settings. Installing a code fix always asks for approval.', anchor: 'activity', symbol: 'check' },
  { title: 'Know what was achieved.', text: 'Activity keeps the findings and measured results. These two outcomes mean different things:', outcomes: true, note: 'Open Details for measurements. If AURA cannot confirm a result, it asks for your help.', anchor: 'activity', symbol: 'activity' },
  { title: 'Add the tools for repairs.', text: 'What’s set up shows which tools are configured. Restoring settings needs a saved working version. Code repairs also need a prepared test and install profile.', route: ['My apps', 'Settings', 'What’s set up'], note: 'Ask the app’s maintainer for a setup file. Load it under Edit connection → Repair setup after pausing watching.', anchor: 'apps', symbol: 'settings' },
  { title: 'Manage the connection.', text: 'Pause watching before editing or removing a connection. Your app keeps running. Its previous checks remain in Activity, including after removal.', route: ['Pause watching', 'Edit or remove'], note: 'Changed settings need a new working baseline. Automatic actions stay off until you enable them again.', anchor: 'apps', symbol: 'apps' },
  { title: 'Make a little room for light.', text: 'Move over the portrait to stir it. Hold to gather particles; release to scatter them. The controls adjust particles, motion and optional sound.', note: 'Pausing the portrait only pauses the animation. App monitoring continues. Reopen this guide with the ? button.', anchor: 'controls', symbol: 'portrait' },
];
const previewSteps: Step[] = [
  { ...portrait, note: 'An offline preview. No app checks or model calls.' },
  { title: 'Set your own pace.', text: 'Adjust particles, motion and volume in Settings. Turn on sound for the glass ambience.', note: 'Pause holds the portrait still. Reset brings the particles home.', anchor: 'controls', symbol: 'settings' },
  { title: 'Ready for your apps?', text: 'Back to apps opens AURA’s workspace, where you can connect an app and investigate problems.', note: 'For now, enjoy the light. You can take this tour again with the ? button.', anchor: 'return', symbol: 'return' },
];

function TourSymbol({ kind }: { kind: Step['symbol'] }) {
  return <svg viewBox="0 0 40 40" fill="none" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {kind === 'portrait' && <><ellipse cx="20" cy="20" rx="11" ry="16" transform="rotate(32 20 20)"/><ellipse cx="20" cy="20" rx="11" ry="16" transform="rotate(-32 20 20)"/><circle cx="20" cy="20" r="2" fill="currentColor"/></>}
    {kind === 'apps' && <><rect x="7" y="9" width="26" height="22" rx="6"/><path d="M7 17h26M20 21v6m-3-3h6"/><circle cx="12" cy="13" r=".6" fill="currentColor"/></>}
    {kind === 'check' && <><circle cx="18" cy="18" r="10"/><path d="m25 25 8 8M12 18h3l2-4 3 8 2-4h2"/></>}
    {kind === 'activity' && <><path d="M10 8v22a3 3 0 0 0 3 3h19M15 23l5-5 5 2 7-9"/><circle cx="32" cy="11" r="2"/></>}
    {kind === 'settings' && <><path d="M7 13h26M7 27h26"/><rect x="13" y="9" width="5" height="8" rx="2.5"/><rect x="24" y="23" width="5" height="8" rx="2.5"/></>}
    {kind === 'return' && <><path d="M28 12H15a7 7 0 0 0-7 7v9M22 6l6 6-6 6"/><path d="M16 28h16"/></>}
  </svg>;
}

/** A local walkthrough only: no app commands, synthetic status or model requests. */
export default function QuickTour({ variant, autoStart = false, onStart, onComplete }: { variant: 'ops' | 'preview'; autoStart?: boolean; onStart: () => void; onComplete?: () => void }) {
  const steps = variant === 'ops' ? opsSteps : previewSteps;
  const storageKey = `aura.tour.${variant}.${variant === 'ops' ? 'v2' : 'v1'}`;
  const [open, setOpen] = useState(false), [index, setIndex] = useState(0);
  const [placement, setPlacement] = useState<{ card: CSSProperties; focus?: CSSProperties; thread?: string }>({ card: {} });
  const dialog = useRef<HTMLDialogElement>(null), card = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  const title = useRef<HTMLHeadingElement>(null), start = useRef(onStart), offered = useRef(false);
  start.current = onStart;
  const step = steps[index];

  function begin() { offered.current = true; start.current(); setIndex(0); setOpen(true); }
  function finish(complete = false) {
    try { localStorage.setItem(storageKey, 'seen'); } catch { /* The tour also works without storage. */ }
    setOpen(false);
    if (complete) onComplete?.();
  }
  useEffect(() => {
    if (!autoStart || offered.current) return;
    try { if (localStorage.getItem(storageKey)) { offered.current = true; return; } } catch { /* Optional preference. */ }
    const timeout = window.setTimeout(() => {
      // Do not interrupt a person who already started using the interface.
      if (document.hidden || document.activeElement?.matches('input, textarea, select, button, a')) return;
      offered.current = true; start.current(); setIndex(0); setOpen(true);
    }, 900);
    return () => clearTimeout(timeout);
  }, [autoStart, storageKey]);
  useLayoutEffect(() => {
    const modal = dialog.current;
    if (!open || !modal) return;
    modal.showModal();
    return () => { modal.close(); trigger.current?.focus({ preventScroll: true }); };
  }, [open]);
  useLayoutEffect(() => {
    if (!open) return;
    title.current?.focus({ preventScroll: true });
    const measure = () => {
      const width = document.documentElement.clientWidth, height = window.innerHeight;
      const small = width < 600, margin = small ? 16 : 28;
      const box = card.current?.getBoundingClientRect();
      if (!box) return;
      const anchor = step.anchor ? document.querySelector<HTMLElement>(`[data-tour="${step.anchor}"]`) : null;
      const rect = anchor?.getBoundingClientRect();
      const dock = document.querySelector('.bottom-bar')?.getBoundingClientRect();
      const maxTop = Math.max(margin, height - box.height - margin);
      let left = small ? (width - box.width) / 2 : Math.max(28, width * .038);
      let top = Math.min(maxTop, (dock?.top ?? height - 80) - box.height - 28);
      if (rect?.width && rect.height) {
        left = Math.max(margin, Math.min(width - box.width - margin, rect.left + rect.width / 2 - box.width / 2));
        top = rect.top - box.height - 26;
        if (top < margin) top = rect.bottom + 26;
        // Leave the face in view on desktop; a fine thread connects the pane to its control.
        if (width >= 900 && step.anchor !== 'return') left = step.anchor === 'controls' ? width - box.width - Math.max(28, width * .038) : Math.max(28, width * .038);
      }
      top = Math.max(margin, Math.min(maxTop, top));
      const endX = rect ? rect.left + rect.width / 2 : 0;
      const startX = Math.max(left + 26, Math.min(left + box.width - 26, endX));
      const above = !!rect && top + box.height < rect.top;
      const startY = above ? top + box.height : top;
      const endY = rect ? above ? rect.top - 6 : rect.bottom + 6 : 0;
      const middleY = (startY + endY) / 2;
      setPlacement({
        card: { left: Math.round(left), top },
        focus: rect?.width && rect.height ? { left: rect.left - 6, top: rect.top - 6, width: rect.width + 12, height: rect.height + 12 } : undefined,
        thread: rect?.width && (above || top > rect.bottom) ? `M${startX} ${startY} C${startX} ${middleY}, ${endX} ${middleY}, ${endX} ${endY}` : undefined,
      });
    };
    measure();
    const observer = new ResizeObserver(measure);
    if (card.current) observer.observe(card.current);
    const anchor = step.anchor ? document.querySelector(`[data-tour="${step.anchor}"]`) : null;
    if (anchor) observer.observe(anchor);
    const dock = document.querySelector('.bottom-bar');
    if (dock) observer.observe(dock);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => { observer.disconnect(); window.removeEventListener('resize', measure); window.removeEventListener('scroll', measure, true); };
  }, [open, step]);

  return <>
    <button ref={trigger} className="icon-button tour-trigger" aria-label="Take a quick tour" title="Take a quick tour" aria-haspopup="dialog" onClick={begin}>
      <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.7 9a2.3 2.3 0 1 1 3.8 1.7c-1 .7-1.5 1-1.5 2.3"/><circle cx="12" cy="16.5" r=".7" fill="currentColor" stroke="none"/></svg>
    </button>
    {open && <dialog ref={dialog} className="quick-tour" aria-labelledby="tour-title" aria-describedby="tour-description" onCancel={e => { e.preventDefault(); finish(); }} onKeyDown={e => {
      if (e.key !== 'Tab') return;
      const controls = [...e.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      const focused = controls.indexOf(document.activeElement as HTMLButtonElement);
      if (e.shiftKey && focused <= 0) { e.preventDefault(); controls.at(-1)?.focus(); }
      else if (!e.shiftKey && (focused < 0 || focused === controls.length - 1)) { e.preventDefault(); controls[0]?.focus(); }
    }}>
      {placement.thread && <svg className="tour-thread" aria-hidden="true"><path d={placement.thread}/></svg>}
      {placement.focus && <div className="tour-focus" style={placement.focus} aria-hidden="true"/>}
      <div ref={card} className="tour-card" style={placement.card}>
        <div className="tour-topline"><span className="tour-symbol"><TourSymbol kind={step.symbol}/></span><button className="tour-skip" onClick={() => finish()}>Skip tour <span aria-hidden="true">×</span></button></div>
        <div key={index} className="tour-copy">
          <h2 ref={title} tabIndex={-1} id="tour-title">{step.title}</h2>
          <p id="tour-description">{step.text}</p>
          {step.route && <ol className="tour-route" aria-label="Steps">{step.route.map((label, i) => <li key={label}>{i > 0 && <span aria-hidden="true">›</span>}<span>{label}</span></li>)}</ol>}
          {step.outcomes && <dl className="tour-outcomes">
            <div><dt><span aria-hidden="true">○</span> App responding again</dt><dd>Access is restored. The cause may remain.</dd></div>
            <div><dt><span aria-hidden="true">✓</span> Problem fixed</dt><dd>A code fix is installed and its result checked.</dd></div>
          </dl>}
          {step.note && <p className="tour-note">{step.note}</p>}
        </div>
        <div className="tour-footer"><div className="tour-progress" aria-label={`Step ${index + 1} of ${steps.length}`}><span>{index + 1}<i aria-hidden="true"> / </i>{steps.length}</span><div aria-hidden="true">{steps.map((_, i) => <i key={i} className={i === index ? 'current' : i < index ? 'done' : ''}/>)}</div></div>
          <div className="tour-navigation">{index > 0 && <button className="tour-back" onClick={() => setIndex(i => i - 1)}>Back</button>}<button className="tour-next" onClick={() => index === steps.length - 1 ? finish(true) : setIndex(i => i + 1)}>{index === steps.length - 1 ? variant === 'ops' && onComplete ? 'Open My apps' : 'Done' : 'Next'}{index < steps.length - 1 && <svg aria-hidden="true" width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="m8 5 5 5-5 5"/></svg>}</button></div>
        </div>
      </div>
    </dialog>}
  </>;
}
