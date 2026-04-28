import fs from 'fs';
import path from 'path';
import os from 'os';
import YAML from 'yaml';

import { configFilePath, reqLogsDir } from './paths.mjs';
import {
  newLayerId,
  createRootLayer,
  createEmptyLayer,
  layerPath,
  readLayerMeta,
  writeLayerMeta,
  LAYER_ID_RE,
  repoDirNameFromUrl,
} from './layerFs.mjs';
import { layersRoot } from './paths.mjs';
import {
  appendExecStream,
  resetExecStream,
  getExecStreamFullText,
  completeExecStream,
} from './execStream.mjs';
import { gitCmd, gitCloneConfigArgs } from './gitCmd.mjs';
import {
  postJson,
  rewriteDockerInternal,
  taskApiPrefix,
  postCloneProgress,
  latestGitProgressPercent,
  parseGitCloneProgressPhases,
  normalizeGitProgressChunkForLog,
  runGitCloneWithProgress,
} from './saasTaskCloud.mjs';

export let bootstrapCloneLayerId = null;
/** 为 true 时 server 须在引导结束后调用 registerBootstrapCloneJob（仅「任务详情已含仓库并完成引导克隆」） */
export let bootstrapRegisterCloneJob = false;
export let startupEmptyLayerId = null;

/**
 * 多仓引导克隆期间：各仓 stderr 并行写入此结构，GET /api/repos/bootstrap-clone-log 再拼成 text 并返回 segments。
 * 引导结束并写入 exec-stream 后清空。
 * @type {{
 *   layerId: string,
 *   preamble: string,
 *   jobs: { raw: string, repoDir: string, index: number }[],
 *   bufs: Map<string, { header: string, body: string, failNote?: string }>,
 * } | null}
 */
let bootstrapRepoLogState = null;

/** 克隆日志走通用 exec-stream（分片 + SSE）；与 GET /api/exec-streams/clone/:id/* 同源 */
export function appendCloneLayerLog(layerId, text) {
  appendExecStream('clone', layerId, text);
}

function rebuildBootstrapParallelLogText() {
  if (!bootstrapRepoLogState) return '';
  const { preamble, jobs, bufs } = bootstrapRepoLogState;
  const parts = [preamble];
  for (const job of jobs) {
    const e = bufs.get(job.raw);
    if (!e) continue;
    parts.push(e.header + e.body + (e.failNote || ''));
  }
  return parts.join('\n\n');
}

/**
 * 引导多仓并行克隆进行中时供 GET /api/repos/bootstrap-clone-log 返回 `segments`（按任务详情仓库顺序）。
 * @param {string} layerId
 * @returns {{ repo_url: string, text: string }[] | null}
 */
export function getBootstrapCloneLogSegmentsForApi(layerId) {
  if (!bootstrapRepoLogState || bootstrapRepoLogState.layerId !== layerId) {
    return null;
  }
  const { jobs, bufs } = bootstrapRepoLogState;
  return jobs.map((job) => {
    const e = bufs.get(job.raw);
    const text = e ? e.header + e.body + (e.failNote || '') : '';
    return { repo_url: job.raw, text };
  });
}

export function getCloneLayerLogText(layerId) {
  if (bootstrapRepoLogState && bootstrapRepoLogState.layerId === layerId) {
    return rebuildBootstrapParallelLogText();
  }
  return getExecStreamFullText('clone', layerId);
}

export function clearCloneLayerLog(layerId) {
  resetExecStream('clone', layerId);
}

/** 引导克隆结束：封包并推送 exec_stream_complete（与 UI 克隆队列一致） */
export function finalizeCloneLayerLog(layerId) {
  completeExecStream('clone', layerId);
}

function businessApiEndpoint() {
  let raw = String(process.env.BusinessApiEndPoint || process.env.BUSINESS_API_ENDPOINT || '').trim();
  if (!raw) {
    throw new Error('BusinessApiEndPoint/BUSINESS_API_ENDPOINT empty');
  }
  return rewriteDockerInternal(raw).replace(/\/$/, '');
}

function skipExchangeForLocalBusinessApi(businessEp) {
  const e = businessEp.toLowerCase();
  const listen = String(process.env.PORT || '8765').trim();
  const pnum = parseInt(listen, 10) || 8765;
  const hosts = [`http://127.0.0.1:${pnum}`, `http://localhost:${pnum}`];
  const hit = hosts.some((h) => e === h || e.startsWith(`${h}/`));
  if (!hit) return false;
  const taskApi = String(process.env.TaskApiEndPoint || '').trim().toLowerCase();
  if (!taskApi) return true;
  return ['127.0.0.1', 'localhost', '::1'].some(
    (h) => taskApi.startsWith(`http://${h}:`) || taskApi.startsWith(`http://${h}/`)
  );
}

function gitCloneRemoteForSshPem(canonicalUrl) {
  let u = String(canonicalUrl || '').trim();
  if (!u) return u;
  const low = u.toLowerCase();
  if (low.startsWith('git@') || low.startsWith('ssh://')) return u;
  if (!low.startsWith('https://')) return u;
  let host = '';
  try {
    host = new URL(u).hostname.toLowerCase();
  } catch {
    return u;
  }
  if (host === 'www.github.com') host = 'github.com';
  let pth = '';
  try {
    pth = new URL(u).pathname.replace(/^\//, '').replace(/\.git$/i, '');
  } catch {
    return u;
  }
  if (!host || !pth || pth.includes('..')) return u;
  return `git@${host}:${pth}.git`;
}

function writeTempSshKey(pem) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'boot_git_'));
  const keyPath = path.join(dir, 'key');
  let c = String(pem).trim();
  if (!c.endsWith('\n')) c += '\n';
  fs.writeFileSync(keyPath, c, { mode: 0o600 });
  return keyPath;
}

function collectRepoUrls(taskDetail) {
  const out = [];
  const seen = new Set();
  function add(raw) {
    if (!raw) return;
    const u = String(raw).trim();
    if (!u || seen.has(u)) return;
    seen.add(u);
    out.push(u);
  }
  function walk(value) {
    if (typeof value === 'string') {
      add(value);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(walk);
      return;
    }
    if (value && typeof value === 'object') {
      add(value.git_repo || value.url || value.repo_url);
      if (value.git_repos != null) walk(value.git_repos);
    }
  }
  if (taskDetail?.project_repos) walk(taskDetail.project_repos);
  if (taskDetail?.git_repos) walk(taskDetail.git_repos);
  const taskObj = taskDetail?.task;
  if (taskObj && typeof taskObj === 'object') {
    if (taskObj.git_repos) walk(taskObj.git_repos);
    const params = taskObj.parameters;
    if (params && typeof params === 'object') {
      for (const k of ['git_repos', 'project_urls', 'project_repos', 'repos', 'repositories']) {
        if (params[k]) walk(params[k]);
      }
    }
  }
  return out;
}

/**
 * 并行发起单仓 git clone；stderr 写入 {@link bootstrapRepoLogState} 中对应仓库的 body（与其它仓并行追加）。
 * @returns {Promise<{ ok: boolean, err?: Error }>}
 */
async function runOneBootstrapClone({
  job,
  n,
  credRoot,
  cloudPrefix,
  accessToken,
}) {
  const { raw, repoDir, index: i } = job;
  const cred = credRoot[raw] || credRoot[raw.replace(/\/$/, '')];
  const pem = cred && typeof cred === 'object' ? String(cred.ephemeral_ssh_private_key || '').trim() : '';
  const useSsh =
    !!pem ||
    String(process.env.GIT_CLONE_USE_SSH_URL || '')
      .trim()
      .toLowerCase() === '1' ||
    ['true', 'yes'].includes(String(process.env.GIT_CLONE_USE_SSH_URL || '').trim().toLowerCase());
  let cloneRemote = raw;
  if (pem || (useSsh && raw.toLowerCase().startsWith('https://'))) {
    cloneRemote = gitCloneRemoteForSshPem(raw);
    if (cloneRemote !== raw) {
      const ent = bootstrapRepoLogState?.bufs.get(raw);
      if (ent) ent.body += `[bootstrap-clone] HTTPS → SSH: ${cloneRemote}\n`;
    }
  }

  let keyPath = null;
  try {
    const gitEnv = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
    const useV4 = String(process.env.TRAE_GIT_CLONE_ALLOW_IPV6 || '').trim() !== '1';
    const args = useV4
      ? [...gitCloneConfigArgs(), 'clone', '-4', '--progress', cloneRemote, repoDir]
      : [...gitCloneConfigArgs(), 'clone', '--progress', cloneRemote, repoDir];
    if (pem) {
      keyPath = writeTempSshKey(pem);
      const sshTimeout = Math.max(
        5,
        Math.min(120, parseInt(String(process.env.TRAE_GIT_SSH_CONNECT_TIMEOUT_SEC || '30'), 10) || 30)
      );
      const ssh = `ssh -i ${keyPath} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=${sshTimeout}`;
      gitEnv.GIT_SSH_COMMAND = ssh;
    }
    let lastPosted = 0;
    let lastPct = -1;
    await runGitCloneWithProgress(args, gitEnv, undefined, (chunk, errAll) => {
      if (chunk) {
        const ent = bootstrapRepoLogState?.bufs.get(raw);
        if (ent) ent.body += normalizeGitProgressChunkForLog(chunk);
      }
      const g = latestGitProgressPercent(errAll);
      if (g < 0) return;
      const now = Date.now();
      if (g === lastPct && now - lastPosted < 2000) return;
      if (now - lastPosted < 400 && g <= lastPct) return;
      lastPct = g;
      lastPosted = now;
      const phases = parseGitCloneProgressPhases(errAll);
      const seg = { phase: 'bootstrap', index: i + 1, total: n };
      if (phases.recv != null) seg.recv_progress = phases.recv;
      if (phases.unpack != null) seg.unpack_progress = phases.unpack;
      void postCloneProgress(
        cloudPrefix,
        accessToken,
        g,
        `【项目克隆】(${i + 1}/${n}) ${path.basename(repoDir)} … ${g}%`,
        raw,
        seg
      );
    });
    await postCloneProgress(
      cloudPrefix,
      accessToken,
      100,
      `项目克隆 (${i + 1}/${n}) 完成 ${path.basename(repoDir)}`,
      raw,
      { phase: 'bootstrap', index: i + 1, total: n, recv_progress: 100, unpack_progress: 100 }
    );
    return { ok: true };
  } catch (err) {
    return { ok: false, err: err instanceof Error ? err : new Error(String(err)) };
  } finally {
    if (keyPath) {
      try {
        fs.rmSync(path.dirname(keyPath), { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
  }
}

async function cloneReposIntoSharedLayer(urls, credRoot, cloudPrefix, accessToken) {
  const trimmed = urls.map((u) => String(u || '').trim()).filter(Boolean);
  if (!trimmed.length) return null;

  /** 与 `ensureStartupEmptyLayer()` 同 id，避免引导克隆层与空层锚点目录并列。 */
  const layerId = startupEmptyLayerId || newLayerId();
  createRootLayer(layerId);
  writeLayerMeta(layerId, 'clone', null);
  clearCloneLayerLog(layerId);
  /** 须在首条日志写入前赋值：克隆可能持续数分钟，期间 GET /api/repos/bootstrap-clone-log 与 /api/project/active 依赖此 id。 */
  bootstrapCloneLayerId = layerId;

  const layerDir = layerPath(layerId);
  const n = trimmed.length;
  /** @type {{ raw: string, repoDir: string, index: number }[]} */
  const jobs = [];
  for (let i = 0; i < trimmed.length; i++) {
    const raw = trimmed[i];
    let repoDir = path.join(layerDir, repoDirNameFromUrl(raw));
    let suf = 2;
    while (fs.existsSync(repoDir)) {
      repoDir = path.join(layerDir, `${path.basename(repoDir)}_${suf}`);
      suf += 1;
    }
    jobs.push({ raw, repoDir, index: i });
  }

  try {
    bootstrapRepoLogState = {
      layerId,
      preamble: '【项目克隆】正在并行克隆任务关联仓库（任务详情已拉取）…\n\n',
      jobs: jobs.slice(),
      bufs: new Map(),
    };
    for (const job of jobs) {
      bootstrapRepoLogState.bufs.set(job.raw, {
        header: `━━ (${job.index + 1}/${n}) ${job.raw}\n→ ${path.basename(job.repoDir)}\n`,
        body: '',
      });
    }

    await postCloneProgress(cloudPrefix, accessToken, 0, '【项目克隆】开始并行克隆任务关联仓库…', null, {
      kind: 'global',
      phase: 'bootstrap',
    });

    for (let i = 0; i < jobs.length; i++) {
      const job = jobs[i];
      await postCloneProgress(
        cloudPrefix,
        accessToken,
        0,
        `【项目克隆】(${i + 1}/${n}) 准备克隆 ${path.basename(job.repoDir)}…`,
        job.raw,
        { phase: 'bootstrap', index: i + 1, total: n }
      );
    }

    const clonePromises = jobs.map((job) =>
      runOneBootstrapClone({
        job,
        n,
        credRoot,
        cloudPrefix,
        accessToken,
      })
    );

    const outcomes = await Promise.all(clonePromises);
    const errors = [];
    for (let idx = 0; idx < jobs.length; idx++) {
      const o = outcomes[idx];
      if (o.ok) continue;
      errors.push(o.err);
      const job = jobs[idx];
      const msg = o.err?.message || String(o.err);
      const ent = bootstrapRepoLogState.bufs.get(job.raw);
      if (ent) {
        ent.failNote = `\n[bootstrap-clone] 克隆失败: ${msg}\n`;
      }
      await postCloneProgress(
        cloudPrefix,
        accessToken,
        0,
        `【项目克隆】(${idx + 1}/${n}) 失败: ${msg.slice(0, 500)}`,
        job.raw,
        { phase: 'bootstrap', index: idx + 1, total: n }
      );
    }

    const footer = errors.length ? '\n【项目克隆】已结束（存在失败）。\n' : '\n【项目克隆】克隆完成。\n';
    const full = rebuildBootstrapParallelLogText() + footer;
    clearCloneLayerLog(layerId);
    appendCloneLayerLog(layerId, full);
    finalizeCloneLayerLog(layerId);
    bootstrapRepoLogState = null;

    if (errors.length) {
      const head = errors[0];
      const msg = head?.message || String(head);
      await postCloneProgress(
        cloudPrefix,
        accessToken,
        0,
        `【项目克隆】未完成：${msg}`.slice(0, 2000),
        null,
        { kind: 'global', phase: 'bootstrap' }
      );
      throw head;
    }
    await postCloneProgress(cloudPrefix, accessToken, 100, '【项目克隆】仓库克隆已完成', null, {
      kind: 'global',
      phase: 'bootstrap',
    });
    return layerId;
  } catch (e) {
    if (bootstrapRepoLogState && bootstrapRepoLogState.layerId === layerId) {
      bootstrapRepoLogState = null;
    }
    throw e;
  }
}

/**
 * 任务详情中无关联仓库时：复用 `ensureStartupEmptyLayer()` 已创建的空层锚点目录，写入 `kind=clone`，
 * 与 `GET /api/layers/empty-root` 为同一 `layer_id`，避免与空锚点并行的多余可写层。
 * 首个仓库由后续 `POST /api/repos/clone`（或等价 git clone）写入子层，父层为上述 id。
 */
function createInitialWorkspaceLayer() {
  const layerId = startupEmptyLayerId || ensureStartupEmptyLayer();
  createRootLayer(layerId);
  writeLayerMeta(layerId, 'clone', null);
  logOutbound(`bootstrap: initial writable layer (reuse empty-root, no git, await clone) ${layerId}`);
  console.log(`[onlineServiceJS] 已复用空层锚点为初始可写层（无 git，待首次克隆）: ${layerId}`);
  return layerId;
}

function ensureReqLog() {
  const d = reqLogsDir();
  return path.join(d, 'outbound.log');
}

function logOutbound(line) {
  try {
    fs.appendFileSync(ensureReqLog(), `${new Date().toISOString()} | ${line}\n`);
  } catch {
    /* ignore */
  }
}

/**
 * HTTP 监听前：解析 TaskApi 前缀并完成换票（若需要）。
 * 任务详情拉取、仓库克隆、service_config.yaml 写入在 {@link runBootstrapAfterListen}。
 */
export async function runBootstrapTokenExchangeOnly() {
  bootstrapCloneLayerId = null;
  bootstrapRepoLogState = null;
  bootstrapRegisterCloneJob = false;
  let prefix;
  try {
    prefix = taskApiPrefix();
  } catch (e) {
    logOutbound(`bootstrap skip: ${e.message}`);
    return { skipped: true };
  }
  if (!prefix) return { skipped: true };

  const timeout = Math.max(1, parseFloat(process.env.TASK_API_BOOTSTRAP_TIMEOUT_SEC || '5') || 5);
  const business = businessApiEndpoint();
  let newAccess = String(process.env.ACCESS_TOKEN || '').trim();
  if (!newAccess) throw new Error('ACCESS_TOKEN empty for bootstrap');

  if (!skipExchangeForLocalBusinessApi(business)) {
    const ex = await postJson(
      `${prefix}/server-container-token/exchange-refresh/`,
      { access_token: newAccess, business_api_endpoint: business },
      timeout
    );
    const refreshToken = ex.refresh_token;
    if (!refreshToken) throw new Error('exchange-refresh missing refresh_token');
    const ref = await postJson(
      `${prefix}/server-container-token/refresh-access/`,
      { refresh_token: refreshToken },
      timeout
    );
    const at = ref.access_token;
    if (!at || typeof at !== 'string') throw new Error('refresh-access missing access_token');
    newAccess = at;
    process.env.ACCESS_TOKEN = newAccess;
  } else {
    logOutbound('bootstrap: skip exchange (local business API)');
  }

  return { skipped: false, prefix, newAccess, timeout };
}

/**
 * 容器已监听端口后：拉取任务详情 → 克隆关联仓库 → 拉取并写入 feature YAML。
 */
export async function runBootstrapAfterListen(ctx) {
  if (!ctx || ctx.skipped) {
    logOutbound('bootstrap post-listen: skip (no task API prefix)');
    return;
  }
  const { prefix, newAccess, timeout } = ctx;
  const timeoutSec = timeout;

  console.log('[onlineServiceJS] 容器已启动，开始拉取任务详情…');
  logOutbound('bootstrap post-listen: task-detail');

  const detail = await postJson(
    `${prefix}/server-container-token/task-detail/`,
    { access_token: newAccess },
    timeoutSec
  );
  const urls = collectRepoUrls(detail);
  let credRoot = {};
  if (detail && typeof detail.repo_clone_credentials === 'object') {
    credRoot = detail.repo_clone_credentials;
  }
  if (urls.length) {
    console.log('[onlineServiceJS] 任务详情已就绪，开始项目克隆…');
    bootstrapCloneLayerId = await cloneReposIntoSharedLayer(urls, credRoot, prefix, newAccess);
    bootstrapRegisterCloneJob = true;
  } else {
    logOutbound('bootstrap: no repo urls in task-detail');
    bootstrapCloneLayerId = createInitialWorkspaceLayer();
    bootstrapRegisterCloneJob = false;
  }

  const y = await postJson(
    `${prefix}/server-container-token/feature-params-yaml/`,
    { access_token: newAccess },
    timeoutSec
  );
  const yamlText = y.yaml;
  if (yamlText == null || typeof yamlText !== 'string') {
    throw new Error('feature-params-yaml missing yaml');
  }
  YAML.parse(yamlText);
  const dest = configFilePath();
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, yamlText, 'utf8');
  logOutbound(`bootstrap: wrote ${dest}`);
  console.log('[onlineServiceJS] 任务引导完成（详情已拉取、克隆与配置已就绪）。');
}

/** 顺序执行换票 + 详情/克隆/配置（单测或无需分离 listen 的场景）。 */
export async function runBootstrap() {
  const ctx = await runBootstrapTokenExchangeOnly();
  if (ctx.skipped) return;
  await runBootstrapAfterListen(ctx);
}

export function ensureStartupEmptyLayer() {
  const root = layersRoot();
  if (!fs.existsSync(root)) fs.mkdirSync(root, { recursive: true });
  for (const name of fs.readdirSync(root).sort()) {
    if (!LAYER_ID_RE.test(name)) continue;
    const m = readLayerMeta(name);
    if (m && m.kind === 'empty') {
      startupEmptyLayerId = name;
      return name;
    }
  }
  const id = newLayerId();
  createEmptyLayer(id);
  startupEmptyLayerId = id;
  return id;
}
