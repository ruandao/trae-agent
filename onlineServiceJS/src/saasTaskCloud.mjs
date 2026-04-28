/**
 * 任务云 TaskApi 前缀、JSON POST，以及克隆进度上报（git-clone-progress → SaaS SSE）。
 * 供 bootstrap、POST /api/repos/reclone 等共用。
 */
import { spawn } from 'child_process';

import { gitCmd } from './gitCmd.mjs';

function traceHeaders() {
  const tid = String(process.env.TRACE_ID || '').trim();
  const h = { 'Content-Type': 'application/json' };
  if (tid) h['X-Trace-Id'] = tid;
  return h;
}

export async function postJson(url, body, timeoutSec = 8) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), timeoutSec * 1000);
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: traceHeaders(),
      body: JSON.stringify(body),
      signal: ac.signal,
    });
    const text = await r.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      throw new Error(`Invalid JSON from ${url}: ${text.slice(0, 200)}`);
    }
    if (!r.ok) {
      throw new Error(`HTTP ${r.status} ${url}: ${JSON.stringify(data).slice(0, 500)}`);
    }
    return data;
  } finally {
    clearTimeout(t);
  }
}

export function rewriteDockerInternal(url) {
  const u = String(url || '').trim();
  if (!u) return u;
  try {
    const x = new URL(u);
    if (x.hostname.toLowerCase() !== 'host.docker.internal') return u;
    const ip = String(process.env.DOCKER_HOST_GATEWAY_IP || '').trim();
    if (!ip) return u;
    x.hostname = ip;
    return x.toString();
  } catch {
    return u;
  }
}

export function taskApiPrefix() {
  const endpoint = rewriteDockerInternal(String(process.env.TaskApiEndPoint || '').trim());
  if (!endpoint) return null;
  const tenant = String(process.env.tenantId || '').trim();
  const workspace = String(process.env.workspaceId || '').trim();
  const task = String(process.env.taskId || '').trim();
  if (!tenant || !workspace || !task) {
    throw new Error('TaskApiEndPoint set but tenantId/workspaceId/taskId missing');
  }
  return `${endpoint.replace(/\/$/, '')}/api/tenant/${tenant}/workspace/${workspace}/task/${task}/cloud`;
}

/**
 * 与 `GET /api/repos/bootstrap-clone-log` 的 `segments[].repo_url` 及 SaaS SSE `segment` 对齐。
 * @param {string|null} repoUrl
 * @param {null|{ kind?: 'repo'|'global', index?: number, total?: number, phase?: string, label?: string, repo_url?: string, recv_progress?: number, unpack_progress?: number }} [segmentExtra]
 */
function buildGitCloneProgressSegment(repoUrl, segmentExtra) {
  const ru = String(repoUrl || '').trim();
  const ex = segmentExtra && typeof segmentExtra === 'object' ? segmentExtra : null;
  const seg = {};
  if (ru) {
    seg.kind = 'repo';
    seg.repo_url = ru.slice(0, 2000);
  } else {
    const fallbackRu = ex && String(ex.repo_url || '').trim();
    if (ex && ex.kind === 'repo' && fallbackRu) {
      seg.kind = 'repo';
      seg.repo_url = fallbackRu.slice(0, 2000);
    } else {
      seg.kind = 'global';
    }
  }
  if (ex) {
    if (typeof ex.index === 'number' && Number.isFinite(ex.index)) {
      seg.index = Math.max(1, Math.floor(ex.index));
    }
    if (typeof ex.total === 'number' && Number.isFinite(ex.total)) {
      seg.total = Math.max(1, Math.floor(ex.total));
    }
    if (typeof ex.phase === 'string' && ex.phase.trim()) {
      seg.phase = ex.phase.trim().slice(0, 48);
    }
    if (typeof ex.label === 'string' && ex.label.trim()) {
      seg.label = ex.label.trim().slice(0, 200);
    }
    for (const k of ['recv_progress', 'unpack_progress']) {
      const n = ex[k];
      if (typeof n === 'number' && Number.isFinite(n)) {
        seg[k] = Math.max(0, Math.min(100, Math.floor(n)));
      }
    }
  }
  return seg;
}

/**
 * 上报至 SaaS `git-clone-progress`；Django 会原样在 SSE 中带 `segment`，与多仓并行克隆一致。
 * @param {string|null} repoUrl
 * @param {null|{ kind?: 'repo'|'global', index?: number, total?: number, phase?: string, label?: string }} [segmentExtra]
 */
export async function postCloneProgress(cloudPrefix, accessToken, progress, message, repoUrl = null, segmentExtra = null) {
  if (!cloudPrefix || !accessToken) return;
  const url = `${cloudPrefix.replace(/\/$/, '')}/server-container-token/git-clone-progress/`;
  const body = {
    access_token: accessToken,
    progress: Math.max(0, Math.min(100, progress)),
    message: String(message || '').slice(0, 2000),
  };
  const ru = String(repoUrl || '').trim();
  if (ru) body.repo_url = ru.slice(0, 2000);
  const segment = buildGitCloneProgressSegment(ru, segmentExtra);
  if (segment && Object.keys(segment).length) {
    body.segment = segment;
  }
  try {
    await postJson(url, body, 10);
  } catch {
    /* optional */
  }
}

/**
 * 解析 `git clone --progress` 的 stderr：区分「接收 pack」与「本地解压/增量」等阶段（与 bootstrap / reclone 一致）。
 * @param {string} stderrAll
 * @returns {{ recv: number|null, unpack: number|null, overall: number }}
 */
export function parseGitCloneProgressPhases(stderrAll) {
  const tail = stderrAll.length > 12000 ? stderrAll.slice(-12000) : stderrAll;
  let bestRecv = -1;
  for (const m of tail.matchAll(/Receiving objects:\s*(\d+)%/g)) {
    const v = parseInt(m[1], 10);
    if (Number.isFinite(v)) bestRecv = Math.max(bestRecv, v);
  }
  const secondary = [
    /Resolving deltas:\s*(\d+)%/g,
    /Unpacking objects:\s*(\d+)%/g,
    /Checking out files:\s*(\d+)%/g,
  ];
  let bestSecondary = -1;
  for (const re of secondary) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(tail)) !== null) {
      const v = parseInt(m[1], 10);
      if (Number.isFinite(v)) bestSecondary = Math.max(bestSecondary, v);
    }
  }
  /** 与历史单条进度一致：优先 Receiving，否则取解压/解算等阶段 */
  let overall = -1;
  if (bestRecv >= 0) overall = bestRecv;
  else if (bestSecondary >= 0) overall = bestSecondary;
  return {
    recv: bestRecv >= 0 ? bestRecv : null,
    unpack: bestSecondary >= 0 ? bestSecondary : null,
    overall: overall >= 0 ? overall : -1,
  };
}

/**
 * 解析 git clone --progress 的 stderr（与 bootstrap 一致）。
 * @param {string} stderrAll
 * @returns {number}
 */
export function latestGitProgressPercent(stderrAll) {
  const { overall } = parseGitCloneProgressPhases(stderrAll);
  return overall;
}

/** git --progress 用 \r 刷行；写入持久克隆日志时换成换行 */
export function normalizeGitProgressChunkForLog(s) {
  return String(s || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

export function runGitCloneWithProgress(args, env, cwd, onStderrProgress) {
  return new Promise((resolve, reject) => {
    const proc = spawn(gitCmd(), args, {
      env: {
        ...process.env,
        ...env,
        GIT_TERMINAL_PROMPT: '0',
        GIT_HTTP_IPV4: String(env.GIT_HTTP_IPV4 || process.env.GIT_HTTP_IPV4 || '1'),
      },
      cwd: cwd || undefined,
    });
    let err = '';
    proc.stderr?.on('data', (c) => {
      const s = c.toString();
      err += s;
      if (onStderrProgress) onStderrProgress(s, err);
    });
    proc.on('error', reject);
    proc.on('close', (code) => {
      if (code === 0) resolve(err);
      else reject(new Error(`git exit ${code}: ${err.slice(-1500)}`));
    });
  });
}
