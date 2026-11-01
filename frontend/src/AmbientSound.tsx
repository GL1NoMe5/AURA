import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { renderGlassTap } from './audio/crystal';

function createSound() {
  const context = new AudioContext();
  const master = context.createGain(); master.gain.value = 0;
  const ambience = context.createGain(); ambience.gain.value = .85;
  const limiter = context.createDynamicsCompressor();
  limiter.threshold.value = -12; limiter.ratio.value = 6;
  ambience.connect(master); master.connect(limiter).connect(context.destination);
  const tapData = renderGlassTap(context.sampleRate);
  const tapBuffer = context.createBuffer(1, tapData.length, context.sampleRate);
  tapBuffer.copyToChannel(tapData, 0);
  const voices = new Set<AudioBufferSourceNode>();
  let lastTap = -1;
  function tap() {
    const time = context.currentTime;
    if (context.state !== 'running' || time - lastTap < .055 || voices.size >= 4) return;
    lastTap = time;
    const voice = context.createBufferSource(); voice.buffer = tapBuffer;
    voice.connect(master); voices.add(voice);
    voice.onended = () => { voices.delete(voice); voice.disconnect(); };
    voice.start();
  }
  const controller = new AbortController();
  const ready = fetch('/audio/aura-crystal.wav?v=crystal-1', { signal: controller.signal })
    .then(response => { if (!response.ok) throw new Error('Audio unavailable'); return response.arrayBuffer(); })
    .then(bytes => context.decodeAudioData(bytes))
    .then(buffer => {
      if (context.state === 'closed') return;
      const cue = context.createBufferSource(); cue.buffer = buffer; cue.loop = true;
      cue.connect(ambience); cue.start();
    });
  return { context, master, ready, tap, abort: () => controller.abort() };
}

type Sound = { enabled: boolean; volume: number; error: string; toggle: () => void; setVolume: (n: number) => void };
const SoundContext = createContext<Sound | null>(null);

export function AmbientProvider({ children }: { children: ReactNode }) {
  const sound = useRef<ReturnType<typeof createSound> | null>(null);
  const [enabled, setEnabled] = useState(false), [volume, setVolume] = useState(.45), [error, setError] = useState('');
  const intent = useRef(false), alive = useRef(true), version = useRef(0);
  const volumeRef = useRef(volume), timer = useRef(0);
  volumeRef.current = volume;

  function mute() {
    intent.current = false; version.current++; setEnabled(false);
    clearTimeout(timer.current);
    const audio = sound.current;
    if (!audio || audio.context.state === 'closed') return;
    audio.master.gain.setTargetAtTime(0, audio.context.currentTime, .08);
    timer.current = window.setTimeout(() => {
      if (!intent.current && sound.current === audio && audio.context.state !== 'closed') void audio.context.suspend().catch(() => {});
    }, 550);
  }
  function toggle() {
    if (intent.current) { sound.current?.tap(); mute(); return; }
    intent.current = true;
    const current = ++version.current;
    clearTimeout(timer.current);
    try {
      sound.current ??= createSound();
      const audio = sound.current;
      void Promise.all([audio.context.resume(), audio.ready]).then(() => {
        if (!alive.current || !intent.current || version.current !== current || document.hidden) return;
        audio.master.gain.setTargetAtTime(volumeRef.current, audio.context.currentTime, .65);
        setError(''); setEnabled(true); audio.tap();
      }).catch(() => {
        if (sound.current !== audio || version.current !== current) return;
        intent.current = false; audio.abort();
        if (audio.context.state !== 'closed') void audio.context.close().catch(() => {});
        sound.current = null;
        if (alive.current) { setEnabled(false); setError('Sound unavailable. Try again.'); }
      });
    } catch { intent.current = false; setError('Sound unavailable in this browser.'); }
  }
  useEffect(() => {
    const audio = sound.current;
    if (audio && audio.context.state !== 'closed') audio.master.gain.setTargetAtTime(enabled ? volume : 0, audio.context.currentTime, .15);
  }, [enabled, volume]);
  useEffect(() => {
    alive.current = true;
    const hide = () => {
      if (!document.hidden) return;
      intent.current = false; version.current++; setEnabled(false); clearTimeout(timer.current);
      const audio = sound.current;
      if (audio && audio.context.state !== 'closed') {
        audio.master.gain.setValueAtTime(0, audio.context.currentTime);
        void audio.context.suspend().catch(() => {});
      }
    };
    const click = (event: MouseEvent) => {
      if (!event.isTrusted || !intent.current || document.hidden || !(event.target instanceof Element)) return;
      const control = event.target.closest('button, summary, input[type="checkbox"], input[type="radio"], [role="switch"], a[href]');
      if (!control || control.closest('[data-aura-sound-toggle]') || control.matches(':disabled, [aria-disabled="true"]')) return;
      // Native keyboard activation also dispatches click; labels forward one click to their input.
      sound.current?.tap();
    };
    document.addEventListener('visibilitychange', hide);
    document.addEventListener('click', click, true);
    return () => {
      alive.current = false; intent.current = false; version.current++; clearTimeout(timer.current);
      document.removeEventListener('visibilitychange', hide); document.removeEventListener('click', click, true);
      const audio = sound.current; sound.current = null; audio?.abort();
      if (audio && audio.context.state !== 'closed') void audio.context.close().catch(() => {});
    };
  }, []);
  return <SoundContext.Provider value={{ enabled, volume, error, toggle, setVolume: n => setVolume(Math.max(0, Math.min(1, n))) }}>{children}</SoundContext.Provider>;
}

export function SoundButton() {
  const sound = useContext(SoundContext)!;
  return <button className="icon-button sound-button" data-aura-sound-toggle aria-label={sound.enabled ? 'Mute sound' : 'Play crystal sound'} aria-pressed={sound.enabled} onClick={sound.toggle}>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5Z"/>{sound.enabled ? <path d="M15 8c3 2 3 6 0 8m3-11c5 4 5 10 0 14"/> : <path d="m16 9 5 6m0-6-5 6"/>}</svg>
  </button>;
}

export function SoundVolume() {
  const sound = useContext(SoundContext)!;
  return <div className="sound-settings">
    <label className="setting-row" data-aura-sound-toggle><span>Crystal sound</span><input className="quiet-switch" type="checkbox" role="switch" checked={sound.enabled} onChange={sound.toggle}/></label>
    <label className="setting-row sound-volume">Volume<input aria-label="Sound volume" type="range" min="0" max="100" value={Math.round(sound.volume * 100)} onChange={e => sound.setVolume(Number(e.target.value) / 100)}/></label>
    {sound.error && <p role="status" className="panel-footnote">{sound.error}</p>}
  </div>;
}
