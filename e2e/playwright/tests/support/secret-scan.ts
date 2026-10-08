import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { ARTIFACTS } from "./env";

/** Entries of a zip file (stored or deflated), read with the central directory. */
function* zipEntries(buf: Buffer): Generator<{ name: string; data: Buffer }> {
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65_557); i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) return;
  const count = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  for (let n = 0; n < count && buf.readUInt32LE(p) === 0x02014b50; n++) {
    const method = buf.readUInt16LE(p + 10);
    const size = buf.readUInt32LE(p + 20);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const local = buf.readUInt32LE(p + 42);
    const name = buf.toString("utf8", p + 46, p + 46 + nameLen);
    p += 46 + nameLen + extraLen + commentLen;
    const start = local + 30 + buf.readUInt16LE(local + 26) + buf.readUInt16LE(local + 28);
    const raw = buf.subarray(start, start + size);
    if (method === 0) yield { name, data: raw };
    else if (method === 8) yield { name, data: zlib.inflateRawSync(raw) };
  }
}

function* files(dir: string): Generator<string> {
  if (!fs.existsSync(dir)) return;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* files(full);
    else yield full;
  }
}

/** Every artifact (and every entry inside a zip) that contains `secret`. */
export function findSecret(secret: string, root = ARTIFACTS): string[] {
  const needle = Buffer.from(secret, "utf8");
  const encoded = Buffer.from(encodeURIComponent(secret), "utf8");
  const hits: string[] = [];
  const has = (b: Buffer) => b.includes(needle) || b.includes(encoded);
  for (const file of files(root)) {
    const buf = fs.readFileSync(file);
    if (file.endsWith(".zip")) {
      for (const entry of zipEntries(buf)) if (has(entry.data)) hits.push(`${path.relative(root, file)}!${entry.name}`);
    } else if (has(buf)) {
      hits.push(path.relative(root, file));
    }
  }
  return hits;
}
