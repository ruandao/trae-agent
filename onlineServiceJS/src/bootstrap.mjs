import fs from 'fs';
import path from 'path';
import os from 'os';
import { spawn } from 'child_process';
import YAML from 'yaml';

import { configFilePath, reqLogsDir } from './paths.mjs';
import {
  newLayerId,
  createRootLayer,
  createEmptyLayer,
  layerPath,
  readLayerMeta,
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

export let bootstrapCloneLayerId = null;
export let startupEmptyLayerId = null;

/** 克隆日志走通用 exec-stream（分片 + SSE）；与 GET /api/exec-streams/clone/:id/* 同源 */
export function appendCloneLayerLog(layerId, text) {
  appendExecStream('clone', layerId, text);
}

export function getCloneLayerLogText(layerId) {
  return getExecStreamFullText('clone', layerId);
}

export function clearCloneLayerLog(layerId) {
  resetExecStream('clone', layerId);
}

/** 引导克隆结束：封包并推送 exec_stream_complete（与 UI 克隆队列一致） */
export function finalizeCloneLayerLog(layerId) {
  completeExecStream('clone', layerId);
}

function traceHeaders() {
  const tid = String(process.env.TRACE_ID || '').trim();
  const h = { 'Content-Type': 'application/json' };
  if (tid) h['X-Trace-Id'] = tid;
  return h;
}

async function postJson(url, body, timeoutSec = 8) {
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

function rewriteDockerInternal(url) {
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

function taskApiPrefix() {
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

function runGitClone(args, env, cwd) {
  return new Promise((resolve, reject) => {
    const proc = spawn(gitCmd(), args, { env: { ...process.env, ...env }, cwd: cwd || undefined });
    let err = '';
    proc.stderr?.on('data', (c) => {
      err += c.toString();
    });
    proc.on('error', reject);
    proc.on('close', (code) => {
      if (code === 0) resolve(err);
      else reject(new Error(`git exit ${code}: ${err.slice(-1500)}`));
    });
  });
}

async function postCloneProgress(cloudPrefix, accessToken, progress, message) {
  const url = `${cloudPrefix.replace(/\/$/, '')}/server-container-token/git-clone-progress/`;
  try {
    await postJson(
      url,
      {
        access_token: accessToken,
        progress: Math.max(0, Math.min(100, progress)),
        message: String(message || '').slice(0, 2000),
      },
      10
    );
  } catch {
    /* optional */
  }
}

async function cloneReposIntoSharedLayer(urls, credRoot, cloudPrefix, accessToken) {
  if (!urls.length) return null;
  const gitBin = 'git';
  const layerId = newLayerId();
  createRootLayer(layerId);
  clearCloneLayerLog(layerId);
  appendCloneLayerLog(layerId, '【容器启动引导】正在克隆任务关联仓库…\n\n');
  await postCloneProgress(cloudPrefix, accessToken, 0, '【容器启动引导】开始克隆任务关联仓库…');

  const layerDir = layerPath(layerId);
  const n = urls.length;
  for (let i = 0; i < urls.length; i++) {
    const raw = urls[i].trim();
    if (!raw) continue;
    let repoDir = path.join(layerDir, repoDirNameFromUrl(raw));
    let suf = 2;
    while (fs.existsSync(repoDir)) {
      repoDir = path.join(layerDir, `${path.basename(repoDir)}_${suf}`);
      suf += 1;
    }
    appendCloneLayerLog(layerId, `━━ (${i + 1}/${n}) ${raw}\n→ ${path.basename(repoDir)}\n`);

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
        appendCloneLayerLog(layerId, `[bootstrap-clone] HTTPS → SSH: ${cloneRemote}\n`);
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
        const ssh = `ssh -i ${keyPath} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new`;
        gitEnv.GIT_SSH_COMMAND = ssh;
      }
      await runGitClone(args, gitEnv);
      const pct = Math.min(99, Math.floor(((i + 1) / n) * 100));
      await postCloneProgress(cloudPrefix, accessToken, pct, `容器克隆 (${i + 1}/${n}) 完成 ${path.basename(repoDir)}`);
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
  appendCloneLayerLog(layerId, '\n【容器启动引导】克隆完成。\n');
  finalizeCloneLayerLog(layerId);
  await postCloneProgress(cloudPrefix, accessToken, 100, '【容器启动引导】仓库克隆已完成');
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

export async function runBootstrap() {
  bootstrapCloneLayerId = null;
  let prefix;
  try {
    prefix = taskApiPrefix();
  } catch (e) {
    logOutbound(`bootstrap skip: ${e.message}`);
    return;
  }
  if (!prefix) return;

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

  const detail = await postJson(
    `${prefix}/server-container-token/task-detail/`,
    { access_token: newAccess },
    timeout
  );
  const urls = collectRepoUrls(detail);
  let credRoot = {};
  if (detail && typeof detail.repo_clone_credentials === 'object') {
    credRoot = detail.repo_clone_credentials;
  }
  if (urls.length) {
    bootstrapCloneLayerId = await cloneReposIntoSharedLayer(urls, credRoot, prefix, newAccess);
  } else {
    logOutbound('bootstrap: no repo urls in task-detail');
  }

  const y = await postJson(
    `${prefix}/server-container-token/feature-params-yaml/`,
    { access_token: newAccess },
    timeout
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
