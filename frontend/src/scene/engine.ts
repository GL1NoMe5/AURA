import * as THREE from 'three';
import { PortraitFlow } from './portraitFlow';
import { MAX_PARTICLES, normalizeParticleCount } from './particleCount';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import noise from './psrdnoise3.glsl?raw';
import vertex from './particles.vert.glsl?raw';
import fragment from './particles.frag.glsl?raw';
import type { SceneSettings, SceneStats } from './types';

export const NODES: { name: string; detail: string; p: [number, number, number] }[] = [
  { name: 'Auth', detail: 'Identity', p: [-4.0, 0.9, 0.0] },
  { name: 'API Gateway', detail: 'Ingress', p: [-2.0, 2.15, -0.25] },
  { name: 'Game Worker', detail: 'Sessions', p: [2.1, 1.75, 0.1] },
  { name: 'DB Primary', detail: 'Persistence', p: [4.0, -0.1, -0.2] },
  { name: 'Cache Layer', detail: 'Retention', p: [1.5, -1.65, 0.35] },
  { name: 'Telemetry', detail: 'Observation', p: [-2.6, -1.5, 0.3] },
];
const COUNT = MAX_PARTICLES;
function randomSequence() {
  let seed = 27102026;
  return () => { seed = (Math.imul(1664525, seed) + 1013904223) >>> 0; return seed / 4294967296; };
}

export function createScene(host: HTMLDivElement, labels: HTMLDivElement[], read: () => SceneSettings,
  report: (stats: SceneStats) => void, fail: (message: string) => void) {
  const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: false, powerPreference: 'high-performance' });
  renderer.setClearColor(0x03080c, 1);
  renderer.setPixelRatio(1);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.info.autoReset = false;
  const gl = renderer.getContext();
  const gpuInfo = gl.getExtension('WEBGL_debug_renderer_info');
  const gpuName: string = gpuInfo ? String(gl.getParameter(gpuInfo.UNMASKED_RENDERER_WEBGL)) : 'Unavailable';
  renderer.domElement.setAttribute('aria-label', 'Interactive particle portrait that disperses into service streams during an incident');
  renderer.domElement.style.touchAction = 'none';
  host.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(43, 1, 0.1, 80);
  camera.position.set(0, 0.15, 11.5);
  const composer = new EffectComposer(renderer);
  const renderPass = new RenderPass(scene, camera);
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.72, 0.65, 0.64);
  const output = new OutputPass();
  composer.addPass(renderPass); composer.addPass(bloom); composer.addPass(output);
  const rng = randomSequence();
  const positions = new Float32Array(COUNT * 3);
  const starts = new Float32Array(COUNT * 3), ends = new Float32Array(COUNT * 3);
  const controls1 = new Float32Array(COUNT * 3), controls2 = new Float32Array(COUNT * 3);
  const seeds = new Float32Array(COUNT * 4), tints = new Float32Array(COUNT * 3);
  const ranks = new Float32Array(COUNT);
  const palette = [new THREE.Color('#9bdcdb'), new THREE.Color('#ddb987'), new THREE.Color('#65bca6')];
  const faultColor = new THREE.Color('#d77c6b');
  for (let i = 0; i < COUNT; i++) {
    ranks[i] = i + 0.5;
    const route = i % 12, from = route % 6, to = (from + (route < 6 ? 1 : 3)) % 6;
    const a = NODES[from].p, b = NODES[to].p;
    const bend = (route % 2 ? 1 : -1) * (0.7 + (route % 3) * 0.22);
    starts.set(a, i * 3); ends.set(b, i * 3);
    controls1.set([a[0] * 0.62 - a[1] * bend * 0.45, a[1] * 0.62 + a[0] * bend * 0.35, 0.4 + bend], i * 3);
    controls2.set([b[0] * 0.62 + b[1] * bend * 0.45, b[1] * 0.62 - b[0] * bend * 0.35, -0.4 - bend], i * 3);
    seeds.set([rng(), rng(), rng(), rng()], i * 4);
    palette[route % 3].toArray(tints, i * 3);
  }
  const geometry = new THREE.BufferGeometry();
  for (const [name, array, size] of [
    ['position', positions, 3], ['aStart', starts, 3], ['aEnd', ends, 3],
    ['aControl1', controls1, 3], ['aControl2', controls2, 3], ['aSeed', seeds, 4], ['aTint', tints, 3],
    ['aRank', ranks, 1],
  ] as const) geometry.setAttribute(name, new THREE.BufferAttribute(array, size));
  geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 14);
  let visibleCount = normalizeParticleCount(read().particleCount);
  geometry.setDrawRange(0, visibleCount);
  const uniforms = { uTime: { value: 0 }, uPortrait: { value: 1 }, uMotion: { value: 1 }, uBrushEnabled: { value: 1 }, uPixelRatio: { value: 1 }, uVisibleCount: { value: visibleCount }, uHole: { value: new THREE.Vector4() }, uBurstGlow: { value: 0 } };
  const portraitPosition = new Float32Array(COUNT * 3), portraitData = new Float32Array(COUNT * 4);
  const portraitColor = new Float32Array(COUNT * 3);
  const flow = new PortraitFlow(portraitPosition);
  const flowAttribute = new THREE.BufferAttribute(flow.offsets, 2).setUsage(THREE.DynamicDrawUsage);
  let uploadPending = true;
  geometry.setAttribute('aDisplacement', flowAttribute);
  geometry.setAttribute('aPortrait', new THREE.BufferAttribute(portraitPosition, 3));
  geometry.setAttribute('aPortraitData', new THREE.BufferAttribute(portraitData, 4));
  geometry.setAttribute('aPortraitColor', new THREE.BufferAttribute(portraitColor, 3));
  let portraitReady = false, disposed = false;
  const assetAbort = new AbortController();
  void (async () => {
    try {
      const responses = await Promise.all(['particles.bin', 'colors.bin'].map(name =>
        fetch(`/portrait/${name}`, { signal: assetAbort.signal, cache: 'no-cache' })));
      if (responses.some(response => !response.ok)) throw new Error('Portrait asset could not be loaded.');
      const [bytes, colorBytes] = await Promise.all(responses.map(response => response.arrayBuffer()));
      if (bytes.byteLength !== COUNT * 10 * 4) throw new Error('Portrait asset has an invalid size.');
      if (colorBytes.byteLength !== COUNT * 3 * 4) throw new Error('Portrait color asset has an invalid size.');
      const data = new Float32Array(bytes);
      const colors = new Float32Array(colorBytes);
      if (!data.every(Number.isFinite)) throw new Error('Portrait asset contains invalid coordinates.');
      if (!colors.every(value => Number.isFinite(value) && value >= 0 && value <= 1)) throw new Error('Portrait asset contains invalid colors.');
      if (disposed) return;
      portraitColor.set(colors);
      for (let i = 0; i < COUNT; i++) {
        portraitPosition.set(data.subarray(i * 10, i * 10 + 3), i * 3);
        portraitData.set(data.subarray(i * 10 + 3, i * 10 + 7), i * 4);
      }
      flow.refreshPositions();
      for (const name of ['aPortrait', 'aPortraitData', 'aPortraitColor']) geometry.getAttribute(name).needsUpdate = true;
      portraitReady = true;
    } catch (error) {
      if (!disposed) fail(error instanceof Error ? error.message : 'Portrait loading failed.');
    }
  })();
  const material = new THREE.ShaderMaterial({ uniforms, vertexShader: noise + '\n' + vertex, fragmentShader: fragment,
    transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, depthTest: false });
  const points = new THREE.Points(geometry, material);
  scene.add(points);
  const ringGeometry = new THREE.TorusGeometry(0.105, 0.0035, 6, 56);
  const coreGeometry = new THREE.SphereGeometry(0.018, 10, 8);
  const rings: THREE.Mesh[] = [], cores: THREE.Mesh[] = [];
  NODES.forEach((node, i) => {
    const color = palette[i % 3];
    const ring = new THREE.Mesh(ringGeometry, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.65 }));
    ring.position.set(...node.p); scene.add(ring); rings.push(ring);
    const core = new THREE.Mesh(coreGeometry, new THREE.MeshBasicMaterial({ color: color.clone().multiplyScalar(3.0) }));
    core.position.copy(ring.position); scene.add(core); cores.push(core);
  });
  let width = 1, height = 1, ratio = 1;
  function resize() {
    width = Math.max(1, host.clientWidth); height = Math.max(1, host.clientHeight);
    ratio = read().quality === 'full' ? 1 : 0.75;
    renderer.setPixelRatio(ratio); composer.setPixelRatio(ratio);
    renderer.setSize(width, height); composer.setSize(width, height);
    uniforms.uPixelRatio.value = ratio;
    camera.aspect = width / height;
    camera.position.z = camera.aspect < 1.2 ? 11.5 * (1.2 / camera.aspect) : 11.5;
    camera.updateProjectionMatrix();
    flow.setAspect(camera.aspect);
  }
  const observer = new ResizeObserver(resize); observer.observe(host); resize();
  const pointer = new THREE.Vector2(), raycaster = new THREE.Raycaster();
  const portraitPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
  const brushPoint = new THREE.Vector3();
  let heldPointer: number | null = null, holdStarted = 0;
  function canInteract() {
    const settings = read();
    return settings.mode === 'portrait' && !settings.paused && !settings.reducedMotion && portraitReady;
  }
  function onPointer(e: PointerEvent) {
    if (!e.isPrimary || !canInteract()) return;
    const rect = host.getBoundingClientRect();
    pointer.set(THREE.MathUtils.clamp((e.clientX - rect.left) / rect.width * 2 - 1, -1, 1), THREE.MathUtils.clamp(1 - (e.clientY - rect.top) / rect.height * 2, -1, 1));
    raycaster.setFromCamera(pointer, camera);
    if (!raycaster.ray.intersectPlane(portraitPlane, brushPoint)) return;
    flow.pointer(brushPoint.x, brushPoint.y);
    if (heldPointer === e.pointerId && !(e.buttons & 1)) endHold(true);
  }
  function onDown(e: PointerEvent) {
    if (e.button !== 0 || !e.isPrimary || !canInteract() || heldPointer !== null) return;
    onPointer(e);
    flow.attract(brushPoint.x, brushPoint.y);
    heldPointer = e.pointerId; holdStarted = performance.now();
    renderer.domElement.setPointerCapture(e.pointerId);
    renderer.domElement.style.cursor = 'grabbing';
    e.preventDefault();
  }
  function endHold(burst: boolean) {
    if (heldPointer === null) return;
    const pointerId = heldPointer; heldPointer = null;
    if (burst && canInteract()) flow.release((performance.now() - holdStarted) / 1000);
    else flow.cancelAttraction();
    if (renderer.domElement.hasPointerCapture(pointerId)) renderer.domElement.releasePointerCapture(pointerId);
    renderer.domElement.style.cursor = '';
  }
  function onUp(e: PointerEvent) { if (heldPointer === e.pointerId && !(e.buttons & 1)) endHold(true); }
  function cancelHold() { endHold(false); flow.leave(); }
  function visibilityChanged() { if (document.hidden) cancelHold(); }
  function leave() { if (heldPointer === null) flow.leave(); }
  host.addEventListener('pointermove', onPointer); host.addEventListener('pointerdown', onDown); host.addEventListener('pointerleave', leave);
  window.addEventListener('pointerup', onUp); window.addEventListener('pointercancel', cancelHold); window.addEventListener('blur', cancelHold);
  renderer.domElement.addEventListener('lostpointercapture', cancelHold);
  document.addEventListener('visibilitychange', visibilityChanged);
  function contextLost(e: Event) { e.preventDefault(); fail('The graphics context was lost. Reload to restore the scene.'); renderer.setAnimationLoop(null); }
  renderer.domElement.addEventListener('webglcontextlost', contextLost);
  renderer.debug.onShaderError = (gl, program) => { console.error(gl.getProgramInfoLog(program)); fail('A particle shader could not compile on this device.'); };
  const timer = new THREE.Timer(); timer.connect(document);
  const projected = new THREE.Vector3();
  let elapsed = 0, frames = 0, interval = 0, cpuTotal = 0, lastReset = read().resetId;
  function frame() {
    timer.update();
    const cpuStart = performance.now();
    const measuredDelta = timer.getDelta();
    const dt = Math.min(measuredDelta, 0.1), settings = read();
    const targetCount = normalizeParticleCount(settings.particleCount);
    visibleCount = settings.reducedMotion ? targetCount : THREE.MathUtils.lerp(visibleCount, targetCount, 1 - Math.exp(-12 * dt));
    if (Math.abs(visibleCount - targetCount) < 1) visibleCount = targetCount;
    const drawCount = Math.ceil(visibleCount);
    geometry.setDrawRange(0, drawCount);
    uniforms.uVisibleCount.value = visibleCount;
    if (!canInteract()) endHold(false);
    if (settings.resetId !== lastReset) { endHold(false); elapsed = 0; flow.reset(); lastReset = settings.resetId; }
    if ((settings.quality === 'full' ? 1 : 0.75) !== ratio) resize();
    if (!settings.paused && !settings.reducedMotion) elapsed += dt;
    if (!settings.paused) {
      if (settings.reducedMotion) flow.reset();
      else flow.update(dt, settings.mode === 'portrait' && portraitReady, drawCount);
      // Skip the full displacement upload while every grain is at rest;
      // one trailing upload flushes the frame that settles.
      if (flow.hasMotion || uploadPending) {
        flowAttribute.needsUpdate = true;
        uploadPending = flow.hasMotion;
      }
    }
    uniforms.uTime.value = elapsed;
    const portraitTarget = settings.mode === 'portrait' && portraitReady ? 1 : 0;
    const step = settings.paused ? 0 : dt;
    uniforms.uPortrait.value = settings.reducedMotion ? portraitTarget : THREE.MathUtils.clamp(uniforms.uPortrait.value + Math.sign(portraitTarget - uniforms.uPortrait.value) * step / 2, 0, 1);
    uniforms.uMotion.value = settings.reducedMotion ? 0 : 1;
    uniforms.uBrushEnabled.value = settings.reducedMotion ? 0 : 1;
    const holeStrength = settings.reducedMotion ? 0 : flow.holeGlow * uniforms.uPortrait.value;
    uniforms.uHole.value.set(flow.holeX, flow.holeY, holeStrength, Math.min(1, flow.holdSeconds / 6));
    uniforms.uBurstGlow.value = settings.reducedMotion ? 0 : flow.burstGlow;
    bloom.strength = THREE.MathUtils.lerp(0.72, 0.28, uniforms.uPortrait.value);
    bloom.strength += holeStrength * 0.16 + flow.burstGlow * 0.10;
    bloom.threshold = THREE.MathUtils.lerp(0.64, 0.95, uniforms.uPortrait.value);
    camera.lookAt(0, 0.1, 0);
    rings.forEach((ring, i) => {
      const pulse = i === (settings.activeNode ?? 4) ? 1 - uniforms.uPortrait.value : 0;
      ring.scale.setScalar(1 + pulse * (0.25 + 0.12 * Math.sin(elapsed * 2)));
      (ring.material as THREE.MeshBasicMaterial).color.copy(palette[i % 3]).lerp(settings.activeNode === undefined ? faultColor : palette[1], pulse);
      ring.quaternion.copy(camera.quaternion);
      (ring.material as THREE.MeshBasicMaterial).opacity = 0.65 * (1 - uniforms.uPortrait.value);
      cores[i].visible = uniforms.uPortrait.value < 0.5;
      projected.copy(ring.position).project(camera);
      if (labels[i]) {
        labels[i].style.transform = `translate3d(${(projected.x * 0.5 + 0.5) * width + 17}px,${(-projected.y * 0.5 + 0.5) * height - 9}px,0)`;
        labels[i].style.opacity = String(0.8 * (1 - uniforms.uPortrait.value));
      }
    });
    renderer.info.reset(); composer.render(dt);
    cpuTotal += performance.now() - cpuStart;
    frames++; interval += measuredDelta;
    if (interval >= 1) {
      report({ fps: Math.round(frames / interval), cpuMs: Math.round(cpuTotal / frames * 10) / 10, gpu: gpuName, points: drawCount, calls: renderer.info.render.calls, geometries: renderer.info.memory.geometries,
        textures: renderer.info.memory.textures, width: Math.round(width * ratio), height: Math.round(height * ratio), portraitReady,
        frames, seconds: interval, heapBytes: (performance as Performance & { memory?: { usedJSHeapSize: number } }).memory?.usedJSHeapSize ?? null });
      frames = 0; interval = 0; cpuTotal = 0;
    }
  }
  renderer.setAnimationLoop(frame);
  return () => {
    disposed = true; assetAbort.abort();
    cancelHold();
    renderer.setAnimationLoop(null); timer.dispose(); observer.disconnect();
    host.removeEventListener('pointermove', onPointer); host.removeEventListener('pointerdown', onDown); host.removeEventListener('pointerleave', leave);
    window.removeEventListener('pointerup', onUp); window.removeEventListener('pointercancel', cancelHold); window.removeEventListener('blur', cancelHold);
    renderer.domElement.removeEventListener('lostpointercapture', cancelHold);
    document.removeEventListener('visibilitychange', visibilityChanged);
    renderer.domElement.removeEventListener('webglcontextlost', contextLost);
    geometry.dispose(); material.dispose(); ringGeometry.dispose(); coreGeometry.dispose();
    [...rings, ...cores].forEach(m => (m.material as THREE.Material).dispose());
    bloom.dispose(); output.dispose(); renderPass.dispose(); composer.dispose(); renderer.dispose();
    renderer.domElement.remove();
  };
}
