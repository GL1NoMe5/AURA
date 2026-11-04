export type PortraitBatch = (values: Float32Array, offset: number) => void;

// A bounded, lossless stream. Publish complete records before the download ends.
export async function consumePortrait(stream: ReadableStream<Uint8Array>, count: number, batch: PortraitBatch) {
  const bytes = new Uint8Array(count * 40);
  const reader = stream.getReader();
  let received = 0, published = 0;
  function publish(end: number) {
    if (end === published) return;
    const values = new Float32Array(bytes.buffer, published * 40, (end - published) * 10);
    for (let i = 0; i < values.length; i++) {
      if (!Number.isFinite(values[i]) || (i % 10 >= 7 && (values[i] < 0 || values[i] > 1))) {
        throw new Error('Invalid portrait data');
      }
    }
    batch(values, published);
    published = end;
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (received + value.length > bytes.length) throw new Error('Portrait exceeds its expected size');
      bytes.set(value, received); received += value.length;
      // About 100 KB over the network before the first visible grains.
      publish(Math.floor(received / (4096 * 40)) * 4096);
    }
    if (received !== bytes.length) throw new Error('Portrait download was incomplete');
    publish(count);
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export async function loadPortrait(url: string, count: number, signal: AbortSignal, batch: PortraitBatch) {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal.aborted) cancel();
  signal.addEventListener('abort', cancel, { once: true });
  let stalled = false;
  const expire = () => { stalled = true; controller.abort(); };
  let timer = setTimeout(expire, 30_000);
  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'force-cache' });
    if (!response.ok || !response.body) throw new Error('Portrait download is unavailable');
    const stream = response.body.pipeThrough(new DecompressionStream('gzip'));
    await consumePortrait(stream, count, (values, offset) => {
      clearTimeout(timer); timer = setTimeout(expire, 30_000);
      batch(values, offset);
    });
  } catch (error) {
    if (stalled) throw new Error('Portrait download stalled');
    throw error;
  } finally {
    clearTimeout(timer);
    signal.removeEventListener('abort', cancel);
    controller.abort();
  }
}
