import { createHash, randomBytes } from 'node:crypto';

const HEX32 = /^[0-9a-f]{32}$/i;

/** Map X-Trace-Id to Tempo-compatible 32-char hex (matches Python/Go). */
export function otelTraceIdHex(externalId) {
  const raw = String(externalId || '').trim();
  const compact = raw.replace(/-/g, '');
  if (HEX32.test(compact)) return compact.toLowerCase();
  return createHash('sha256').update(raw).digest('hex').slice(0, 32);
}

export function randomSpanIdHex() {
  return randomBytes(8).toString('hex');
}
