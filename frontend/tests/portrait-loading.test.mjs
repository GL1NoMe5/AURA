import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
import { consumePortrait, loadPortrait } from '../src/scene/portraitLoader.ts';

const folder = new URL('../public/portrait/', import.meta.url);
const manifest = JSON.parse(readFileSync(new URL('stream.json', folder), 'utf8'));
const compressed = readFileSync(new URL(manifest.file, folder));
const packed = gunzipSync(compressed);
function chunks(bytes, size = 65537) {
  return new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += size) controller.enqueue(bytes.subarray(i, i + size));
    controller.close();
  }});
}

test('compressed portrait preserves every rendered float and original particle identity', async () => {
  assert.equal(createHash('sha256').update(compressed).digest('hex'), manifest.sha256);
  assert.equal(manifest.count, 120000);
  assert.equal(packed.length, manifest.count * 40);
  const source = readFileSync(new URL('particles.bin', folder));
  const colors = readFileSync(new URL('colors.bin', folder));
  let loaded = 0;
  await consumePortrait(chunks(packed), manifest.count, (values, offset) => {
    assert.equal(offset, loaded);
    const bytes = Buffer.from(values.buffer, values.byteOffset, values.byteLength);
    for (let i = 0; i < values.length / 10; i++) {
      const k = offset + i;
      assert.deepEqual(bytes.subarray(i * 40, i * 40 + 28), source.subarray(k * 40, k * 40 + 28));
      assert.deepEqual(bytes.subarray(i * 40 + 28, i * 40 + 40), colors.subarray(k * 12, k * 12 + 12));
    }
    loaded += values.length / 10;
  });
  assert.equal(loaded, manifest.count);
  assert.ok(compressed.length < (source.length + colors.length) * 0.55);
});

test('first grains are published while the rest of the network stream is still pending', async () => {
  let controller, notify;
  const first = new Promise(resolve => { notify = resolve; });
  const stream = new ReadableStream({ start(value) { controller = value; } });
  let completed = false;
  const loading = consumePortrait(stream, 8192, (_values, offset) => notify(offset)).then(() => { completed = true; });
  controller.enqueue(packed.subarray(0, 4096 * 40));
  assert.equal(await first, 0);
  assert.equal(completed, false);
  controller.enqueue(packed.subarray(4096 * 40, 8192 * 40)); controller.close();
  await loading;
});

test('truncated, oversized, non-finite and invalid colour data are rejected', async () => {
  await assert.rejects(consumePortrait(chunks(packed.subarray(0, 39)), 1, () => {}), /incomplete/);
  await assert.rejects(consumePortrait(chunks(packed.subarray(0, 41)), 1, () => {}), /expected size/);
  for (const [index, value] of [[0, NaN], [7, -0.1], [9, 1.1]]) {
    const invalid = Buffer.from(packed.subarray(0, 40)); invalid.writeFloatLE(value, index * 4);
    await assert.rejects(consumePortrait(chunks(invalid, 17), 1, () => {}), /Invalid portrait/);
  }
});

test('loader decompresses streamed content and reuses its fingerprinted browser cache', async t => {
  let observed;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    observed = { url, options };
    return new Response(chunks(compressed, 12031));
  });
  let loaded = 0;
  await loadPortrait('/portrait/' + manifest.file, manifest.count, new AbortController().signal,
    (values, offset) => { loaded = offset + values.length / 10; });
  assert.equal(loaded, manifest.count);
  assert.equal(observed.options.cache, 'force-cache');
  assert.ok(observed.url.includes(manifest.sha256.slice(0, 16)));
});

test('loader surfaces unavailable files and honours an aborted scene', async t => {
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    options.signal.throwIfAborted();
    return new Response('', { status: 404 });
  });
  await assert.rejects(loadPortrait('/missing', 1, new AbortController().signal, () => {}), /unavailable/);
  const abort = new AbortController(); abort.abort();
  await assert.rejects(loadPortrait('/cancelled', 1, abort.signal, () => {}), { name: 'AbortError' });
});
