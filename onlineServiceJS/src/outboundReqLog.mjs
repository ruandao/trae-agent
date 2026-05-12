import fs from 'fs';
import path from 'path';

import { reqLogsDir } from './paths.mjs';

const DEFAULT_REQ_LOG = 'outbound.log';
/** `git push` 类操作摘要日志（非 HTTP fetch） */
export const GIT_PUSH_REQ_LOG_FILE = 'git-push.log';
/** 允许写入 reqLogs 下的日志文件名（防路径穿越） */
const ALLOWED_REQ_LOG_FILES = new Set([DEFAULT_REQ_LOG, 'heartbeat.log', GIT_PUSH_REQ_LOG_FILE]);

function reqLogBasename(filename) {
  const raw = String(filename ?? '').trim();
  if (!raw) return DEFAULT_REQ_LOG;
  const base = path.basename(raw);
  return ALLOWED_REQ_LOG_FILES.has(base) ? base : DEFAULT_REQ_LOG;
}

/**
 * 出站 HTTP 日志行写入 `{ONLINE_PROJECT_STATE_ROOT}/reqLogs/` 下指定文件（默认 outbound.log）。
 * 仅写摘要行，勿写入 API Key、access_token 等敏感正文。
 * @param {string} line
 * @param {{ filename?: string }} [opts] — `filename` 须为白名单内 basename：`outbound.log` | `heartbeat.log` | `git-push.log`
 */
export function appendOutboundReqLog(line, opts = {}) {
  try {
    const f = path.join(reqLogsDir(), reqLogBasename(opts.filename));
    fs.appendFileSync(f, `${new Date().toISOString()} | ${line}\n`);
  } catch {
    /* ignore */
  }
}

/** 本地 `git push` 等落盘 `{stateRoot}/reqLogs/git-push.log`（勿写私钥或 token） */
export function appendGitPushReqLog(line) {
  appendOutboundReqLog(line, { filename: GIT_PUSH_REQ_LOG_FILE });
}

/** 出站日志用 URL：脱敏常见 query 参数，避免 token 误入磁盘 */
export function sanitizeUrlForOutboundLog(url) {
  const raw = String(url || '').trim();
  if (!raw) return '';
  try {
    const u = new URL(raw.includes('://') ? raw : `http://${raw}`);
    const sensitive = new Set([
      'access_token',
      'token',
      'password',
      'refresh_token',
      'code',
      'client_secret',
      'api_key',
    ]);
    for (const k of [...u.searchParams.keys()]) {
      if (sensitive.has(k.toLowerCase())) u.searchParams.set(k, '(redacted)');
    }
    if (u.username || u.password) {
      u.username = '';
      u.password = '';
    }
    return u.toString();
  } catch {
    return raw.slice(0, 800);
  }
}
