// Decrypts data.enc, written by seasonkit/crypt.mjs: gzip, then AES-256-GCM with a key derived
// from the passphrase by PBKDF2-SHA256. Layout: "HQ1" | version (1 byte) | iterations (uint32 BE)
// | salt (16) | iv (12) | ciphertext with its 16-byte tag.
'use strict';

async function hqDecrypt(buffer, passphrase) {
  const b = new Uint8Array(buffer);
  if (b.length < 52 || b[0] !== 0x48 || b[1] !== 0x51 || b[2] !== 0x31) throw new Error('not an HQ data file');
  if (b[3] !== 1) throw new Error('unknown data file version ' + b[3]);
  const iterations = new DataView(buffer, 4, 4).getUint32(0);
  const salt = b.slice(8, 24), iv = b.slice(24, 36), body = b.slice(36);
  const subtle = (globalThis.crypto || {}).subtle;
  if (!subtle) throw new Error('this browser cannot decrypt (needs HTTPS)');
  const base = await subtle.importKey('raw', new TextEncoder().encode(passphrase), 'PBKDF2', false, ['deriveKey']);
  const key = await subtle.deriveKey({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations }, base,
    { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
  let plain;
  try {
    plain = await subtle.decrypt({ name: 'AES-GCM', iv }, key, body);
  } catch (e) {
    const err = new Error('wrong passphrase');
    err.code = 'badkey';
    throw err;
  }
  const stream = new Blob([plain]).stream().pipeThrough(new DecompressionStream('gzip'));
  return await new Response(stream).text();
}

if (typeof module !== 'undefined') module.exports = { hqDecrypt };
