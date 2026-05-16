/**
 * 任务云 TaskApi 前缀、JSON POST，克隆进度上报（git-clone-progress → SaaS SSE），
 * 层级快照上报（layer-graph-push → SSE container_layer_graph，供任务详情 zTree），
 * 以及容器存活心跳（heartbeat/ → SSE container_heartbeat，供任务详情「容器连接」状态）。
 * 供 bootstrap、POST /api/repos/reclone、jobsRuntime、server 启动后定时循环等共用。
 */
import { spawn } from 'child_process';

import { gitCmd } from './gitCmd.mjs';
import { appendOutboundReqLog, sanitizeUrlForOutboundLog } from './outboundReqLog.mjs';

function traceHeaders() {
  const tid = String(process.env.TRACE_ID || '').trim();
  const h = { 'Content-Type': 'application/json' };
  if (tid) h['X-Trace-Id'] = tid;
  return h;
}

/** 容器心跳 POST 的 reqLogs 文件名（与其它出站 outbound.log 分离） */
export const HEARTBEAT_REQ_LOG_FILE = 'heartbeat.log';

/**
 * @param {string} url
 * @param {object} body
 * @param {number} [timeoutSec]
 * @param {{ reqLogFile?: string }} [opts] — `reqLogFile: 'heartbeat.log'` 时写入 reqLogs/heartbeat.log
 */
export async function postJson(url, body, timeoutSec = 8, opts = {}) {
  const reqLogFile = opts && typeof opts === 'object' ? opts.reqLogFile : undefined;
  const safeUrl = sanitizeUrlForOutboundLog(url);
  const t0 = Date.now();
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), timeoutSec * 1000);
  const logOpts = reqLogFile ? { filename: reqLogFile } : {};
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: traceHeaders(),
      body: JSON.stringify(body),
      signal: ac.signal,
    });
    const text = await r.text();
    const ms = Date.now() - t0;
    appendOutboundReqLog(`postJson POST ${safeUrl} -> HTTP ${r.status} ${ms}ms`, logOpts);
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
  } catch (e) {
    const ms = Date.now() - t0;
    appendOutboundReqLog(
      `postJson POST ${safeUrl} -> error ${String(e?.message || e).slice(0, 400)} ${ms}ms`,
      logOpts,
    );
    throw e;
  } finally {
    clearTimeout(t);
  }
}

/** 将 TaskApi URL 中与 DOCKER_GATEWAY_HOSTNAME 一致的主机名换为 DOCKER_HOST_GATEWAY_IP（均在容器 env 中可选配置）。 */
export function rewriteDockerInternal(url) {
  const u = String(url || '').trim();
  if (!u) return u;
  const gatewayHost = String(process.env.DOCKER_GATEWAY_HOSTNAME || '').trim().toLowerCase();
  if (!gatewayHost) return u;
  try {
    const x = new URL(u);
    if (x.hostname.toLowerCase() !== gatewayHost) return u;
    const ip = String(process.env.DOCKER_HOST_GATEWAY_IP || '').trim();
    if (!ip) return u;
    x.hostname = ip;
    return x.toString();
  } catch {
    return u;
  }
}

/**
 * 从路径中解析任务云三段 ID。
 * 支持：标准任务云路径、`/api/.../task-detail/{id}`、以及浏览器任务详情页 `/tenant/.../task-detail/{id}`。
 * 误把任务详情页 URL 当作 TaskApiEndPoint 时，此前无法解析导致换票跳过、库中 container_refresh_token 一直为空。
 */
function parseTenantWorkspaceTaskFromPath(pathname) {
  const p = String(pathname || '');
  const patterns = [
    /\/api\/tenant\/([^/]+)\/workspace\/([^/]+)\/task\/([^/]+)/,
    /\/api\/tenant\/([^/]+)\/workspace\/([^/]+)\/task-detail\/([^/]+)/,
    /\/tenant\/([^/]+)\/workspace\/([^/]+)\/task-detail\/([^/]+)/,
  ];
  for (const re of patterns) {
    const m = p.match(re);
    if (m) return { tenant: m[1], workspace: m[2], task: m[3] };
  }
  return null;
}

/**
 * 任务云回调前缀：`.../api/tenant/.../workspace/.../task/.../cloud`。
 * 优先读环境变量 tenantId / workspaceId / taskId；若缺失则从 TaskApiEndPoint（或同义的 TASK_API_ENDPOINT）的 URL 路径解析（适用于 UserData 仅注入完整 API 根路径的场景）。
 */
export function taskApiPrefix() {
  const raw = rewriteDockerInternal(
    String(process.env.TaskApiEndPoint || process.env.TASK_API_ENDPOINT || '').trim(),
  );
  if (!raw) return null;

  let tenant = String(process.env.tenantId || '').trim();
  let workspace = String(process.env.workspaceId || '').trim();
  let task = String(process.env.taskId || '').trim();

  try {
    const base = raw.includes('://') ? raw : `http://${raw}`;
    const u = new URL(base);
    const parsed = parseTenantWorkspaceTaskFromPath(u.pathname);
    if (parsed) {
      if (!tenant) tenant = parsed.tenant;
      if (!workspace) workspace = parsed.workspace;
      if (!task) task = parsed.task;
    }
  } catch {
    /* 非 URL 形态时仅依赖环境变量 */
  }

  if (!tenant || !workspace || !task) {
    throw new Error(
      'TaskApiEndPoint/TASK_API_ENDPOINT set but tenantId/workspaceId/taskId missing (请在容器环境注入 tenantId/workspaceId/taskId，或使用可解析路径：/api/tenant/.../task/... 或 /api/.../task-detail/... 或浏览器任务页 /tenant/.../task-detail/...)'
    );
  }

  let origin = raw;
  try {
    const base = raw.includes('://') ? raw : `http://${raw}`;
    const u = new URL(base);
    origin = u.origin;
  } catch {
    throw new Error('Invalid TaskApiEndPoint/TASK_API_ENDPOINT (expected absolute URL or origin)');
  }

  return `${origin.replace(/\/$/, '')}/api/tenant/${tenant}/workspace/${workspace}/task/${task}/cloud`;
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
 * 将当前层级快照上报至 SaaS `server-container-token/layer-graph-push/`，由 Django 推送到
 * `server-startup-status-sse`（`status: container_layer_graph`），任务详情评论区 zTree 可即时刷新。
 * 环境与 `taskApiPrefix()`、`ACCESS_TOKEN` 不全或请求失败时静默忽略。
 * @param {null|{ layers?: unknown[], jobs?: unknown[], layers_root?: string, bootstrap_layer_id?: string|null }} snapshot
 */
/**
 * POST `server-container-token/heartbeat/`：Django 向任务详情 SSE 转发 `container_heartbeat`（前端「容器连接」状态）。
 * 与 layer-graph-push 独立；此前未调用时 zTree 有数据但心跳始终收不到。
 * @param {string} [message]
 * @returns {Promise<boolean>} 是否上报成功（失败静默，供定时循环使用）
 */
let containerHeartbeatSeq = 0;
let lastSaasHeartbeatSeq = 0;

/** 测试或重启后重置 seq/ack 状态 */
export function resetContainerHeartbeatSeqState() {
  containerHeartbeatSeq = 0;
  lastSaasHeartbeatSeq = 0;
}

export async function postContainerHeartbeatToSaas(message) {
  let cloudPrefix;
  try {
    cloudPrefix = taskApiPrefix();
  } catch {
    return false;
  }
  const accessToken = String(process.env.ACCESS_TOKEN || '').trim();
  if (!cloudPrefix || !accessToken) return false;
  const url = `${cloudPrefix.replace(/\/$/, '')}/server-container-token/heartbeat/`;
  containerHeartbeatSeq += 1;
  const body = { access_token: accessToken, seq: containerHeartbeatSeq };
  if (lastSaasHeartbeatSeq > 0) {
    body.ack = lastSaasHeartbeatSeq;
  }
  const msg = typeof message === 'string' ? message.trim() : '';
  if (msg) body.message = msg.slice(0, 500);
  try {
    const data = await postJson(url, body, 10, { reqLogFile: HEARTBEAT_REQ_LOG_FILE });
    const saasSeq = data?.seq;
    if (typeof saasSeq === 'number' && Number.isFinite(saasSeq) && saasSeq >= 0) {
      lastSaasHeartbeatSeq = saasSeq;
    }
    return Boolean(data?.bidirectional_ok ?? data?.status === 'ok');
  } catch {
    return false;
  }
}

const DEFAULT_SAAS_HEARTBEAT_INTERVAL_SEC = 20;

/**
 * 启动定时向 SaaS 上报容器存活（与 `TRAE_SKIP_REACHABILITY_REGISTER` 独立；可单独用 `TRAE_SKIP_SAAS_HEARTBEAT` 关闭）。
 * @returns {() => void} 停止定时器
 */
export function startSaasContainerHeartbeatLoop() {
  if (['1', 'true', 'yes', 'on'].includes(String(process.env.TRAE_SKIP_SAAS_HEARTBEAT || '').toLowerCase())) {
    return () => {};
  }
  const raw = String(process.env.TRAE_SAAS_HEARTBEAT_INTERVAL_SEC || '').trim();
  const sec = Math.max(5, Number.isFinite(parseFloat(raw)) ? parseFloat(raw) : DEFAULT_SAAS_HEARTBEAT_INTERVAL_SEC);
  const tick = () => {
    void postContainerHeartbeatToSaas('onlineServiceJS');
  };
  tick();
  const id = setInterval(tick, Math.round(sec * 1000));
  return () => clearInterval(id);
}

export async function publishLayerGraphSnapshotToSaas(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return;
  let cloudPrefix;
  try {
    cloudPrefix = taskApiPrefix();
  } catch {
    return;
  }
  const accessToken = String(process.env.ACCESS_TOKEN || '').trim();
  if (!cloudPrefix || !accessToken) return;
  const url = `${cloudPrefix.replace(/\/$/, '')}/server-container-token/layer-graph-push/`;
  const body = {
    access_token: accessToken,
    layers: Array.isArray(snapshot.layers) ? snapshot.layers : [],
    jobs: Array.isArray(snapshot.jobs) ? snapshot.jobs : [],
  };
  const lr = snapshot.layers_root;
  if (typeof lr === 'string' && lr.trim()) body.layers_root = lr.trim();
  const bs = snapshot.bootstrap_layer_id;
  if (bs != null && String(bs).trim()) body.bootstrap_layer_id = String(bs).trim();
  try {
    await postJson(url, body, 15);
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

const GIT_CLONE_RETRYABLE_PATTERNS = [
  /RPC failed; curl \d+/i,
  /GnuTLS recv error/i,
  /SSL read: errno/i,
  /OpenSSL SSL_read:/i,
  /SSL_ERROR_SYSCALL/i,
  /fatal: early EOF/i,
  /unexpected disconnect while reading sideband packet/i,
  /fetch-pack: invalid index-pack output/i,
  /Connection (?:timed out|reset by peer)/i,
  /Operation timed out/i,
  /The remote end hung up unexpectedly/i,
];

/**
 * git clone 失败时是否属于可恢复网络抖动：仅此类错误才建议重试。
 * @param {unknown} errLike
 * @returns {boolean}
 */
export function isRetryableGitCloneFailure(errLike) {
  const msg = String(errLike instanceof Error ? errLike.message : errLike || '');
  if (!msg) return false;
  return GIT_CLONE_RETRYABLE_PATTERNS.some((re) => re.test(msg));
}

/**
 * 克隆重试配置（用于 bootstrap/reclone）：默认最多 3 次（含首轮）。
 * @returns {{ maxAttempts: number, backoffMs: number }}
 */
export function gitCloneRetryConfigFromEnv() {
  const maxAttemptsRaw = parseInt(String(process.env.TRAE_GIT_CLONE_MAX_ATTEMPTS || '3'), 10);
  const backoffRaw = parseInt(String(process.env.TRAE_GIT_CLONE_RETRY_BACKOFF_MS || '1200'), 10);
  const maxAttempts = Number.isFinite(maxAttemptsRaw) ? Math.max(1, Math.min(6, maxAttemptsRaw)) : 3;
  const backoffMs = Number.isFinite(backoffRaw) ? Math.max(200, Math.min(15000, backoffRaw)) : 1200;
  return { maxAttempts, backoffMs };
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
