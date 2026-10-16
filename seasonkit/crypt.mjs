// Encrypts the page data for the public site, and decrypts it again for checks.
//
//   SITE_PASSPHRASE=... node seasonkit/crypt.mjs encrypt site/data.json site/data.enc
//   SITE_PASSPHRASE=... node seasonkit/crypt.mjs decrypt site/data.enc out.json
//
// gzip, then AES-256-GCM with a key from PBKDF2-SHA256 over the passphrase and a random salt.
// site/crypto.js reads the same layout in the browser:
//   "HQ1" | version (1 byte) | iterations (uint32 BE) | salt (16) | iv (12) | ciphertext + tag
import { readFileSync, writeFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { pathToFileURL } from "node:url";
import { gzipSync, gunzipSync } from "node:zlib";

const subtle = webcrypto.subtle;
const ITERATIONS = 250000;

async function deriveKey(passphrase, salt, iterations) {
  const base = await subtle.importKey("raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]);
  return subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, base,
    { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}

export async function encrypt(plain, passphrase, iterations = ITERATIONS) {
  const salt = webcrypto.getRandomValues(new Uint8Array(16));
  const iv = webcrypto.getRandomValues(new Uint8Array(12));
  const key = await deriveKey(passphrase, salt, iterations);
  const body = new Uint8Array(await subtle.encrypt({ name: "AES-GCM", iv }, key, gzipSync(plain, { level: 9 })));
  const head = new Uint8Array(8);
  head.set([0x48, 0x51, 0x31, 1]);
  new DataView(head.buffer).setUint32(4, iterations);
  return Buffer.concat([head, salt, iv, body]);
}

export async function decrypt(buf, passphrase) {
  const b = new Uint8Array(buf);
  if (b[0] !== 0x48 || b[1] !== 0x51 || b[2] !== 0x31 || b[3] !== 1) throw new Error("not an HQ data file");
  const iterations = new DataView(b.buffer, b.byteOffset + 4, 4).getUint32(0);
  const key = await deriveKey(passphrase, b.slice(8, 24), iterations);
  const plain = await subtle.decrypt({ name: "AES-GCM", iv: b.slice(24, 36) }, key, b.slice(36));
  return gunzipSync(Buffer.from(plain));
}

async function main() {
  const [cmd, src, dst] = process.argv.slice(2);
  const passphrase = process.env.SITE_PASSPHRASE;
  if (!passphrase || passphrase.length < 12) {
    console.error("SITE_PASSPHRASE is missing or shorter than 12 characters");
    process.exit(2);
  }
  if (cmd === "encrypt") {
    const out = await encrypt(readFileSync(src), passphrase);
    writeFileSync(dst, out);
    console.log(`encrypted ${src} -> ${dst} (${out.length} bytes)`);
  } else if (cmd === "decrypt") {
    writeFileSync(dst, await decrypt(readFileSync(src), passphrase));
    console.log(`decrypted ${src} -> ${dst}`);
  } else {
    console.error("usage: node crypt.mjs encrypt|decrypt <in> <out>");
    process.exit(2);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
