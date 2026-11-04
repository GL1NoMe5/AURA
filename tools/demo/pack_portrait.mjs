// Lossless transport for the existing portrait. No resampling or colour changes.
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { gzipSync } from 'node:zlib';

const folder = new URL('../../frontend/public/portrait/', import.meta.url);
const source = readFileSync(new URL('particles.bin', folder));
const colors = readFileSync(new URL('colors.bin', folder));
const count = source.length / 40;
if (!Number.isInteger(count) || colors.length !== count * 12) throw new Error('Invalid portrait sources');
// Each record contains position (3), portrait data (4), linear RGB (3).
// Unused legacy eye/mouth animation fields are omitted; all rendered floats stay exact.
const packed = Buffer.alloc(count * 40);
for (let i = 0; i < count; i++) {
  source.copy(packed, i * 40, i * 40, i * 40 + 28);
  colors.copy(packed, i * 40 + 28, i * 12, i * 12 + 12);
}
const compressed = gzipSync(packed, { level: 9 });
const hash = createHash('sha256').update(compressed).digest('hex');
// Keep .bin: servers may assign Content-Encoding to .gz and decompress it twice.
const file = `portrait-${hash.slice(0, 16)}.bin`;
writeFileSync(new URL(file, folder), compressed);
writeFileSync(new URL('stream.json', folder), JSON.stringify({ version: 1, count, stride: 10,
  file, sha256: hash, bytes: compressed.length, decoded_bytes: packed.length }, null, 2) + '\n');
console.log(`Portrait: ${source.length + colors.length} → ${compressed.length} bytes, ${count} unchanged particles`);
