import fs from 'fs';
import path from 'path';

import { logsDir } from './paths.mjs';

export function parseInitLogEnvKeysPolicy(raw) {
  const text = String(raw ?? '').trim();
  if (!text) return null;
  const keys = text
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean);
  if (keys.length === 0) return null;
  return new Set(keys);
}

export function buildInitLogEnvSnapshot(envMapping, rawPolicy) {
  const policy = parseInitLogEnvKeysPolicy(rawPolicy);
  const out = {};
  const source = envMapping && typeof envMapping === 'object' ? envMapping : {};

  for (const [rawKey, value] of Object.entries(source)) {
    const key = String(rawKey ?? '').trim();
    if (!key) continue;
    if (policy && !policy.has(key)) continue;
    out[key] = String(value);
  }

  return out;
}

export function buildInitLogRecord({ pid, port, envMapping, now, rawPolicy }) {
  const ts = (now instanceof Date ? now : new Date(now ?? Date.now())).toISOString();
  return `${JSON.stringify({
    ts,
    event: 'onlineServiceJS.init',
    pid,
    port: String(port ?? ''),
    env: buildInitLogEnvSnapshot(envMapping, rawPolicy),
  })}\n`;
}

export function appendInitLogBestEffort(
  { pid, port, envMapping, now, rawPolicy },
  { writeFile = fs.appendFileSync } = {}
) {
  try {
    const file = path.join(logsDir(), 'init.log');
    writeFile(file, buildInitLogRecord({ pid, port, envMapping, now, rawPolicy }));
    return { ok: true, error: null };
  } catch (error) {
    return { ok: false, error };
  }
}
