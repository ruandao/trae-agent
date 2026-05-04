import fs from 'fs';
import path from 'path';
import os from 'os';
import { fileURLToPath } from 'url';
import express from 'express';
import multer from 'multer';
import YAML from 'yaml';
import { spawn, spawnSync } from 'child_process';

import { authMiddleware, accessTokenExpected } from './auth.mjs';
import { getAgentRenderHints } from './agentRenderHints.mjs';
import { serviceRoot, configFilePath, repoRoot, logsDir } from './paths.mjs';
import { ssePingLoop, addSseClient, broadcast } from './sseHub.mjs';
import {
  runBootstrapTokenExchangeOnly,
  runBootstrapAfterListen,
  bootstrapCloneLayerId,
  bootstrapRegisterCloneJob,
  ensureStartupEmptyLayer,
  getCloneLayerLogText,
  getBootstrapCloneLogSegmentsForApi,
  clearCloneLayerLog,
  startupEmptyLayerId,
  appendCloneLayerLog,
} from './bootstrap.mjs';
import {
  taskApiPrefix,
  postCloneProgress,
  latestGitProgressPercent,
  parseGitCloneProgressPhases,
  normalizeGitProgressChunkForLog,
  runGitCloneWithProgress,
} from './saasTaskCloud.mjs';
import {
  getExecStreamManifest,
  getExecStreamSegment,
  validExecStreamKind,
  validExecStreamResourceId,
} from './execStream.mjs';
import { enqueueClone, getCloneOpStatus } from './cloneQueue.mjs';
import {
  layerPath,
  newLayerId,
  anyLayerHasGitRepo,
  listLayerRows,
  layerPrimaryGitWorkdir,
  listFlatRelativeFilesForLayer,
  resolveLayerGitLogContext,
  resolveAbsolutePathForLayerListedFile,
  deleteLayerTree,
  directChildLayerIds,
  repoDirNameFromUrl,
  writeLayerMeta,
  readLayerMeta,
  resolvedParentLayerId,
  layerGitWorkdirRootsForFileListing,
  createStackedLayer,
} from './layerFs.mjs';

import {
  createJob,
  listJobs,
  getJob,
  jobToApiDict,
  interruptJob,
  deleteJob,
  registerBootstrapCloneJob,
  buildLayersSnapshot,
  mirrorLayerGraphToTaskCloudSSE,
  sweepDanglingLayerDirs,
  enqueueLayerQueueItem,
  removeLayerQueue,
  getJobEvents,
} from './jobsRuntime.mjs';
import { getJobStepsForLayer } from './jobSteps.mjs';
import { getLayerParentDiffFiles, getLayerParentUnifiedDiff } from './layerParentDiff.mjs';
import { gitCmd, gitCloneConfigArgs } from './gitCmd.mjs';
import { suggestStagedCommitMessage } from './stagedCommitSuggest.mjs';
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const TRACE_HEADER = 'X-Trace-Id';

function traceMiddleware(req, res, next) {
  const tid = (req.headers[TRACE_HEADER.toLowerCase()] || '').toString().trim() || cryptoRandomId();
  res.setHeader(TRACE_HEADER, tid);
  next();
}

function cryptoRandomId() {
  return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
}

/** 避免 UI/localStorage 里残留非 PEM 文本时仍走 SSH，把公开 HTTPS 误转为 git@ 导致克隆失败 */
function looksLikePemPrivateKey(raw) {
  const s = String(raw || '').trim();
  if (s.length < 40) return false;
  return /-----BEGIN[^-]+PRIVATE KEY-----/.test(s) && /-----END[^-]+KEY-----/.test(s);
}

function gitSshCommandFromIdentityFile(resolvedPath) {
  return `ssh -i ${resolvedPath} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new`;
}

function useGitCloneForceIpv4() {
  return String(process.env.TRAE_GIT_CLONE_ALLOW_IPV6 || '').trim() !== '1';
}

function buildGitCloneArgs(cloneUrl, { branch, depth }) {
  const args = [...gitCloneConfigArgs(), 'clone'];
  // Docker/部分网络下对 github.com 等优先走 IPv6 会连不上，强制 -4 可稳定 HTTPS/SSH 克隆
  if (useGitCloneForceIpv4()) {
    args.push('-4');
  }
  args.push('--progress');
  if (depth != null && Number.isFinite(depth) && depth > 0) {
    args.push('--depth', String(Math.floor(depth)));
  }
  if (branch) {
    args.push('--branch', branch);
  }
  args.push(cloneUrl, '.');
  return args;
}

const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 50 * 1024 * 1024 } });

async function gitExec(args, cwd, env = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(gitCmd(), args, { cwd, env: { ...process.env, ...env, GIT_TERMINAL_PROMPT: '0' } });
    let out = '';
    let err = '';
    proc.stdout?.on('data', (c) => {
      out += c.toString();
    });
    proc.stderr?.on('data', (c) => {
      err += c.toString();
    });
    proc.on('error', reject);
    proc.on('close', (code) => {
      if (code === 0) resolve(out + err);
      else reject(new Error((err || out || `git exit ${code}`).slice(-4000)));
    });
  });
}

/** 将仓内相对路径规范为安全 pathspec（防 .. 与越界），失败返回 null */
function safeRepoRelativePathForGitAdd(work, relPath) {
  const relNorm = String(relPath || '')
    .replace(/\\/g, '/')
    .replace(/^\/+/, '');
  if (!relNorm) return null;
  const parts = relNorm.split('/').filter((p) => p.length);
  if (!parts.length || parts.some((p) => p === '.' || p === '..')) return null;
  const candidate = path.resolve(path.join(work, ...parts));
  const w = path.resolve(work);
  if (candidate !== w && !candidate.startsWith(w + path.sep)) return null;
  return relNorm;
}

const app = express();
app.use(traceMiddleware);
app.use(express.json({ limit: '20mb' }));

const accessLogPath = () => path.join(logsDir(), 'requests.log');
function logReq(req, res, start, err) {
  try {
    const ms = ((Date.now() - start) / 1000).toFixed(2);
    const line = `${req.ip || '-'} "${req.method} ${req.originalUrl}" ${err ? 'err' : res.statusCode} ${ms}ms\n`;
    fs.mkdirSync(path.dirname(accessLogPath()), { recursive: true });
    fs.appendFileSync(accessLogPath(), `${new Date().toISOString()} | ${line}`);
  } catch {
    /* ignore */
  }
}
app.use((req, res, next) => {
  const s = Date.now();
  res.on('finish', () => logReq(req, res, s, false));
  next();
});

app.get('/skill.md', (req, res) => {
  const p = path.join(serviceRoot(), 'skill.md');
  if (!fs.existsSync(p)) return res.status(404).send('missing');
  res.type('text/markdown; charset=utf-8').send(fs.readFileSync(p, 'utf8'));
});

app.get('/ui/:access_token', (req, res) => {
  const expected = accessTokenExpected();
  if (!expected || req.params.access_token !== expected) {
    return res.status(401).json({ detail: 'Invalid or missing access token' });
  }
  const staticIndex = path.join(serviceRoot(), 'static', 'index.html');
  if (!fs.existsSync(staticIndex)) {
    return res
      .status(200)
      .type('html')
      .send(
        `<!DOCTYPE html><html><head><meta charset="utf-8"><title>onlineServiceJS</title></head><body><p>onlineServiceJS 已就绪。仓库中应包含 <code>onlineServiceJS/static</code>（见 Dockerfile）；缺失时请从构建上下文恢复该目录，或使用任务云任务详情。</p></body></html>`
      );
  }
  let raw = fs.readFileSync(staticIndex, 'utf8');
  raw = raw.replace('__ACCESS_TOKEN_JSON__', JSON.stringify(req.params.access_token));
  res.type('html').send(raw);
});

/** 新窗口查看「富文本呈现声明」JSON（与 GET /api/ui/agent-render-hints 同源数据） */
app.get('/ui/:access_token/render-hints', (req, res) => {
  const expected = accessTokenExpected();
  if (!expected || req.params.access_token !== expected) {
    return res.status(401).json({ detail: 'Invalid or missing access token' });
  }
  const p = path.join(serviceRoot(), 'static', 'render-hints.html');
  if (!fs.existsSync(p)) {
    return res.status(404).type('text/plain').send('render-hints.html missing');
  }
  let raw = fs.readFileSync(p, 'utf8');
  raw = raw.replace('__ACCESS_TOKEN_JSON__', JSON.stringify(req.params.access_token));
  res.type('html').send(raw);
});

app.use('/static', express.static(path.join(serviceRoot(), 'static')));

const api = express.Router();
api.use(authMiddleware);

api.get('/events/stream', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders?.();
  addSseClient(res);
  res.write(`data: ${JSON.stringify({ type: 'connected' })}\n\n`);
});

api.post('/config', upload.single('file'), (req, res) => {
  const buf = req.file?.buffer;
  if (!buf?.length) return res.status(400).json({ detail: 'Empty file' });
  try {
    YAML.parse(buf.toString('utf8'));
  } catch (e) {
    return res.status(400).json({ detail: String(e.message || e) });
  }
  const dest = configFilePath();
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, buf);
  res.json({ path: dest, status: 'ok' });
});

api.post('/config/raw', (req, res) => {
  const yaml = (req.query.yaml || '').toString();
  if (!yaml.trim()) return res.status(400).json({ detail: 'yaml required' });
  try {
    YAML.parse(yaml);
  } catch (e) {
    return res.status(400).json({ detail: String(e.message || e) });
  }
  const dest = configFilePath();
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, yaml, 'utf8');
  res.json({ path: dest, status: 'ok' });
});

api.get('/config', (req, res) => {
  const dest = configFilePath();
  if (!fs.existsSync(dest)) return res.status(404).json({ detail: 'not found' });
  res.json({ path: dest, yaml: fs.readFileSync(dest, 'utf8') });
});

api.get('/requirements/task-gate', (req, res) => {
  res.json({ clone_done: anyLayerHasGitRepo() });
});

/** Agent 步骤字段 → 富文本呈现策略（表驱动）；前端 GET 后按 step_rows / tool_expansion / tail_rows 渲染 */
api.get('/ui/agent-render-hints', (req, res) => {
  res.json(getAgentRenderHints());
});

api.get('/layers/empty-root', (req, res) => {
  res.json({ layer_id: startupEmptyLayerId });
});

api.get('/layers', (req, res) => {
  const snap = buildLayersSnapshot(bootstrapCloneLayerId);
  res.json({
    layers: snap.layers,
    layers_root: snap.layers_root,
    bootstrap_layer_id: snap.bootstrap_layer_id,
  });
});

api.post('/layers', async (req, res) => {
  const parentLayerId = req.body?.parent_layer_id ? String(req.body.parent_layer_id).trim() : '';
  if (!parentLayerId) {
    return res.status(400).json({ detail: 'parent_layer_id 必填' });
  }
  const known = new Set(listLayerRows().map((r) => r.layer_id));
  if (!known.has(parentLayerId)) {
    return res.status(404).json({ detail: 'parent layer not found' });
  }

  const lid = newLayerId();
  const root = layerPath(lid);

  try {
    createStackedLayer(lid, parentLayerId);

    // 根据层类型和提交信息设置元数据
    const layerKind = req.body?.layer_kind ? String(req.body.layer_kind).trim() : 'job';
    const commitMessage = req.body?.commit_message ? String(req.body.commit_message).trim() : '';

    // 如果是 git commit 类型，设置特殊的元数据
    if (layerKind === 'git_commit' && commitMessage) {
      const metaPath = path.join(root, 'layer_meta.json');
      if (fs.existsSync(metaPath)) {
        let meta = {};
        try {
          meta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
        } catch {
          meta = {};
        }
        meta.kind = 'git_commit';
        meta.commit_message = commitMessage;
        fs.writeFileSync(metaPath, JSON.stringify(meta, null, 2), 'utf8');
      }
    }
    await mirrorLayerGraphToTaskCloudSSE();
    res.status(201).json({
      layer_id: lid,
      layer_path: root,
      parent_layer_id: parentLayerId,
      kind: layerKind,
    });
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.get('/jobs', (req, res) => {
  res.json({ jobs: listJobs().map(jobToApiDict) });
});

api.get('/jobs/:job_id', (req, res) => {
  const j = getJob(req.params.job_id);
  if (!j) return res.status(404).json({ detail: 'not found' });
  res.json(jobToApiDict(j));
});

api.get('/jobs/:job_id/steps', (req, res) => {
  const j = getJob(req.params.job_id);
  if (!j) return res.status(404).json({ detail: 'not found' });
  const payload = getJobStepsForLayer(j.layer_id, j.id);
  res.json(payload);
});

api.get('/jobs/:job_id/events', (req, res) => {
  const j = getJob(req.params.job_id);
  if (!j) return res.status(404).json({ detail: 'not found' });
  const offset = parseInt(req.query.offset || '0', 10) || 0;
  const limit = parseInt(req.query.limit || '500', 10) || 500;
  const result = getJobEvents(req.params.job_id, offset, limit);
  res.json(result);
});

api.get('/jobs/:job_id/parent', (req, res) => {
  const j = getJob(req.params.job_id);
  if (!j) return res.status(404).json({ detail: 'not found' });
  const p = j.parent_job_id ? getJob(j.parent_job_id) : null;
  res.json({ parent: p ? jobToApiDict(p) : null });
});

api.post('/jobs', async (req, res) => {
  try {
    const rec = await createJob(req.body || {});
    res.status(201).json(jobToApiDict(rec));
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.post('/jobs/:job_id/interrupt', (req, res) => {
  try {
    const rec = interruptJob(req.params.job_id);
    res.json(jobToApiDict(rec));
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.delete('/jobs/:job_id', (req, res) => {
  try {
    res.json(deleteJob(req.params.job_id));
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.post('/jobs/:job_id/redo', (req, res) => {
  res.status(501).json({ detail: 'onlineServiceJS: redo 尚未实现，请新建任务或在本仓库补齐该端点' });
});

api.post('/jobs/:job_id/continue', (req, res) => {
  res.status(501).json({ detail: 'onlineServiceJS: continue 尚未实现' });
});

api.post('/jobs/reset', async (req, res) => {
  const layerIds = listLayerRows().map((r) => r.layer_id);
  for (const j of [...listJobs()]) {
    try {
      deleteJob(j.id);
    } catch {
      /* ignore */
    }
  }
  for (const lid of layerIds) {
    try {
      deleteLayerTree(lid);
    } catch {
      /* ignore */
    }
  }
  await mirrorLayerGraphToTaskCloudSSE().catch(() => {});
  res.json({ jobs_cleared: true, layers_removed: layerIds });
});

api.get('/repos/clone-log/:layer_id', (req, res) => {
  const lid = req.params.layer_id;
  res.json({ layer_id: lid, text: getCloneLayerLogText(lid) });
});

/** 通用执行流：总览（分片列表，JSON）；后续其他 kind（如 job）共用同一路径 */
api.get('/exec-streams/:kind/:resourceId/manifest', (req, res) => {
  const { kind, resourceId } = req.params;
  if (!validExecStreamKind(kind) || !validExecStreamResourceId(resourceId)) {
    return res.status(400).json({ detail: 'invalid kind or resource_id' });
  }
  const manifest = getExecStreamManifest(kind, resourceId);
  res.json(manifest);
});

api.get('/exec-streams/:kind/:resourceId/segments/:seq', (req, res) => {
  const { kind, resourceId, seq } = req.params;
  if (!validExecStreamKind(kind) || !validExecStreamResourceId(resourceId)) {
    return res.status(400).json({ detail: 'invalid kind or resource_id' });
  }
  const seg = getExecStreamSegment(kind, resourceId, seq);
  if (!seg) {
    return res.status(404).json({ detail: 'segment not found' });
  }
  res.json(seg);
});

api.get('/repos/clone-status/:layer_id', (req, res) => {
  const lid = req.params.layer_id;
  const st = getCloneOpStatus(lid);
  if (st) {
    return res.json({ layer_id: lid, ...st });
  }
  res.json({ layer_id: lid, status: 'unknown' });
});

api.get('/repos/bootstrap-clone-log', (req, res) => {
  const lid = bootstrapCloneLayerId;
  const text = lid ? getCloneLayerLogText(lid) : '';
  const segments = lid ? getBootstrapCloneLogSegmentsForApi(lid) : null;
  const payload = { layer_id: lid, text };
  if (segments && segments.length) {
    payload.segments = segments;
  }
  res.json(payload);
});

api.post('/repos/clone', (req, res) => {
  const url = String(req.body?.url || '').trim();
  if (!url) return res.status(400).json({ detail: 'url required' });
  const parent_layer_id = req.body?.parent_layer_id ? String(req.body.parent_layer_id).trim() : '';
  const pemRaw = String(req.body?.ephemeral_ssh_private_key ?? '');
  const usePem = looksLikePemPrivateKey(pemRaw);
  const pem = usePem ? pemRaw.trim() : '';

  const sshIdentityIn = req.body?.ssh_identity_file ? String(req.body.ssh_identity_file).trim() : '';
  let sshIdentityResolved = null;
  if (sshIdentityIn) {
    sshIdentityResolved = path.resolve(sshIdentityIn);
    try {
      if (!fs.existsSync(sshIdentityResolved) || !fs.statSync(sshIdentityResolved).isFile()) {
        return res.status(400).json({
          detail: `ssh_identity_file 不存在或不是文件: ${sshIdentityIn}`,
        });
      }
    } catch (e) {
      return res.status(400).json({ detail: `ssh_identity_file: ${e.message || e}` });
    }
  }

  const branch = req.body?.branch ? String(req.body.branch).trim() : '';
  let depth = null;
  if (req.body?.depth != null && req.body?.depth !== '') {
    const d = parseInt(String(req.body.depth), 10);
    if (!Number.isFinite(d) || d < 1) {
      return res.status(400).json({ detail: 'depth 须为正整数' });
    }
    depth = d;
  }

  const lid = newLayerId();
  const root = layerPath(lid);
  let ephemeralKeyDir = null;
  try {
    // 在克隆开始前先创建层级节点，建立可写层
    writeLayerMeta(lid, 'clone', parent_layer_id || null);
    fs.mkdirSync(root, { recursive: true });
    const cloneCwd = path.join(root, 'base');
    fs.mkdirSync(cloneCwd, { recursive: true });
    clearCloneLayerLog(lid);

    let cloneUrl = url;
    const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
    if (usePem) {
      cloneUrl = url.toLowerCase().startsWith('https://') ? gitSshFromHttps(url) : url;
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ui_clone_'));
      ephemeralKeyDir = dir;
      const keyPath = path.join(dir, 'k');
      let c = pem;
      if (!c.endsWith('\n')) c += '\n';
      fs.writeFileSync(keyPath, c, { mode: 0o600 });
      env.GIT_SSH_COMMAND = gitSshCommandFromIdentityFile(keyPath);
    } else if (sshIdentityResolved) {
      cloneUrl = url.toLowerCase().startsWith('https://') ? gitSshFromHttps(url) : url;
      env.GIT_SSH_COMMAND = gitSshCommandFromIdentityFile(sshIdentityResolved);
    } else {
      cloneUrl = url;
    }

    const gitArgs = buildGitCloneArgs(cloneUrl, { branch, depth });
    const queuePosition = enqueueClone({
      lid,
      root,
      cloneCwd,
      parentLayerId: parent_layer_id || null,
      gitArgs,
      env,
      ephemeralKeyDir,
      titleUrl: url,
    });

    res.status(202).json({
      accepted: true,
      status: 'queued',
      layer_id: lid,
      layer_path: root,
      queue_position: queuePosition,
    });
  } catch (e) {
    try {
      fs.rmSync(root, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
    if (ephemeralKeyDir) {
      try {
        fs.rmSync(ephemeralKeyDir, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
    res.status(400).json({ detail: String(e.message || e), exit_code: 1 });
  }
});

function gitSshFromHttps(url) {
  try {
    const u = new URL(url);
    let host = u.hostname.toLowerCase();
    if (host === 'www.github.com') host = 'github.com';
    let pth = u.pathname.replace(/^\//, '').replace(/\.git$/i, '');
    if (!host || !pth || pth.includes('..')) return url;
    return `git@${host}:${pth}.git`;
  } catch {
    return url;
  }
}

api.post('/repos/reclone', async (req, res) => {
  const repoUrl = String(req.body?.repo_url || '').trim();
  if (!repoUrl) return res.status(400).json({ detail: 'repo_url required' });
  const pemRaw = String(req.body?.ephemeral_ssh_private_key ?? '');
  const usePem = looksLikePemPrivateKey(pemRaw);
  const pem = usePem ? pemRaw.trim() : '';
  let layerId = bootstrapCloneLayerId;
  if (!layerId) {
    for (const row of listLayerRows()) {
      if (layerPrimaryGitWorkdir(row.layer_id)) {
        layerId = row.layer_id;
        break;
      }
    }
  }
  if (!layerId) return res.status(400).json({ detail: '引导克隆层不存在' });
  const name = repoDirNameFromUrl(repoUrl);
  let target = path.join(layerPath(layerId), name);
  if (fs.existsSync(target)) fs.rmSync(target, { recursive: true, force: true });
  fs.mkdirSync(target, { recursive: true });
  const env = {
    ...process.env,
    GIT_TERMINAL_PROMPT: '0',
    GIT_HTTP_IPV4: String(process.env.GIT_HTTP_IPV4 || '1'),
  };
  let cloneUrl = repoUrl;
  let ephemeralKeyDir = null;
  try {
    if (usePem) {
      cloneUrl = repoUrl.toLowerCase().startsWith('https://') ? gitSshFromHttps(repoUrl) : repoUrl;
      ephemeralKeyDir = fs.mkdtempSync(path.join(os.tmpdir(), 'reclone_'));
      const keyPath = path.join(ephemeralKeyDir, 'k');
      let c = pem;
      if (!c.endsWith('\n')) c += '\n';
      fs.writeFileSync(keyPath, c, { mode: 0o600 });
      env.GIT_SSH_COMMAND = gitSshCommandFromIdentityFile(keyPath);
    }
  } catch (e) {
    if (ephemeralKeyDir) {
      try {
        fs.rmSync(ephemeralKeyDir, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
    return res.status(400).json({ detail: String(e.message || e) });
  }

  const gitArgs = buildGitCloneArgs(cloneUrl, { branch: '', depth: null });
  let prefix = null;
  try {
    prefix = taskApiPrefix();
  } catch {
    prefix = null;
  }
  const accessToken = String(process.env.ACCESS_TOKEN || '').trim();

  const runRecloneInBackground = () => {
    void (async () => {
      try {
        if (prefix && accessToken) {
          await postCloneProgress(prefix, accessToken, 0, `【重新克隆】开始 ${name}…`, repoUrl, {
            phase: 'reclone',
          });
        }
        try {
          appendCloneLayerLog(layerId, `\n━━ 重新克隆 ${repoUrl}\n→ ${name}\n`);
        } catch {
          /* ignore */
        }
        let lastPosted = 0;
        let lastPct = -1;
        await runGitCloneWithProgress(gitArgs, env, target, (chunk, errAll) => {
          if (chunk) {
            try {
              appendCloneLayerLog(layerId, normalizeGitProgressChunkForLog(chunk));
            } catch {
              /* ignore */
            }
          }
          if (!prefix || !accessToken) return;
          const g = latestGitProgressPercent(errAll);
          if (g < 0) return;
          const now = Date.now();
          if (g === lastPct && now - lastPosted < 2000) return;
          if (now - lastPosted < 400 && g <= lastPct) return;
          lastPct = g;
          lastPosted = now;
          const phases = parseGitCloneProgressPhases(errAll);
          const seg = { phase: 'reclone' };
          if (phases.recv != null) seg.recv_progress = phases.recv;
          if (phases.unpack != null) seg.unpack_progress = phases.unpack;
          void postCloneProgress(prefix, accessToken, g, `【重新克隆】${name} … ${g}%`, repoUrl, seg);
        });
        try {
          const metaPath = path.join(layerPath(layerId), 'layer_meta.json');
          const existingMeta = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
          existingMeta.clone_url = String(repoUrl).trim();
          fs.writeFileSync(metaPath, JSON.stringify(existingMeta, null, 2), 'utf8');
        } catch {
          /* ignore */
        }
        if (prefix && accessToken) {
          await postCloneProgress(prefix, accessToken, 100, `【重新克隆】完成 ${name}`, repoUrl, {
            phase: 'reclone',
            recv_progress: 100,
            unpack_progress: 100,
          });
        }
        try {
          appendCloneLayerLog(layerId, `\n[重新克隆] 完成 ${name}\n`);
        } catch {
          /* ignore */
        }
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        try {
          appendCloneLayerLog(layerId, `\n[重新克隆] 失败: ${msg}\n`);
        } catch {
          /* ignore */
        }
        if (prefix && accessToken) {
          await postCloneProgress(
            prefix,
            accessToken,
            0,
            `【重新克隆】失败: ${msg.slice(0, 500)}`,
            repoUrl,
            { phase: 'reclone' }
          );
        }
        try {
          fs.rmSync(target, { recursive: true, force: true });
        } catch {
          /* ignore */
        }
      } finally {
        if (ephemeralKeyDir) {
          try {
            fs.rmSync(ephemeralKeyDir, { recursive: true, force: true });
          } catch {
            /* ignore */
          }
        }
      }
    })();
  };

  res.status(202).json({
    accepted: true,
    status: 'started',
    layer_id: layerId,
    repo_url: repoUrl,
    message: '重新克隆已在后台进行，进度经任务 SSE 推送',
  });
  setImmediate(runRecloneInBackground);
});

api.delete('/layers/:layer_id', (req, res) => {
  const lid = req.params.layer_id;
  for (const cid of directChildLayerIds(lid)) {
    removeLayerQueue(cid);
    deleteLayerTree(cid);
  }
  removeLayerQueue(lid);
  deleteLayerTree(lid);
  res.json({ ok: true });
});

api.post('/layers/:layer_id/queue', (req, res) => {
  try {
    const out = enqueueLayerQueueItem(req.params.layer_id, req.body || {});
    res.status(201).json(out);
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.get('/layers/:layer_id/files', (req, res) => {
  const maxCap = Math.min(Math.max(1, parseInt(req.query.max_files || '2000', 10) || 2000), 5000);
  const files = listFlatRelativeFilesForLayer(req.params.layer_id, maxCap);
  res.json({ files });
});

api.get('/layers/:layer_id/files/*', (req, res) => {
  const lid = req.params.layer_id;
  const rel = req.params[0] || '';
  const fp = resolveAbsolutePathForLayerListedFile(lid, rel);
  if (!fp) return res.status(404).json({ detail: 'not found' });
  const max = Math.min(parseInt(req.query.max_bytes || '2000000', 10) || 2000000, 20_000_000);
  const buf = fs.readFileSync(fp).subarray(0, max);
  const text = buf.toString('utf8');
  res.json({ path: rel, content: text, truncated: buf.length >= max });
});

api.get('/layers/:layer_id/children', (req, res) => {
  const work = layerPrimaryGitWorkdir(req.params.layer_id);
  if (!work) {
    return res.json({ entries: [], total: 0, next_offset: 0, truncated: false });
  }
  const workResolved = path.resolve(work);
  const dirRaw = (req.query.dir ?? '').toString().trim();
  const dirRel = dirRaw.replace(/\\/g, '/').replace(/^\/+/, '');
  const absDir = path.resolve(path.join(work, dirRel || '.'));
  if (absDir !== workResolved && !absDir.startsWith(workResolved + path.sep)) {
    return res.status(400).json({ detail: 'invalid dir' });
  }
  const prefixRaw = (req.query.prefix ?? '').toString().replace(/\\/g, '/');
  const offset = Math.max(0, parseInt(req.query.offset ?? '0', 10) || 0);
  const limit = Math.min(Math.max(1, parseInt(req.query.limit ?? '200', 10) || 200), 2000);

  let dirents = [];
  try {
    dirents = fs.readdirSync(absDir, { withFileTypes: true });
  } catch (e) {
    return res.status(400).json({ detail: String(e.message || e) });
  }

  function normalizeRel(p) {
    return String(p || '')
      .replace(/\\/g, '/')
      .replace(/^\/+|\/+$/g, '');
  }

  function gitStatusPathSets(workDir) {
    const cwd = String(workDir || '').trim();
    if (!cwd) return { staged: new Set(), unstaged: new Set(), deleted: new Set() };
    const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };

    // 获取 git status --porcelain 来检查哪些是已删除的
    const statusPorcelain = (() => {
      try {
        const out = spawnSync(gitCmd(), ['status', '--porcelain'], {
          cwd,
          encoding: 'utf8',
          env,
          maxBuffer: 32 * 1024 * 1024,
        });
        return String(out.stdout || '');
      } catch {
        return '';
      }
    })();

    const deleted = new Set();
    for (const line of statusPorcelain.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const status = trimmed.slice(0, 2);
      const pathPart = trimmed.slice(3);
      const normalizedPath = normalizeRel(pathPart);
      if (normalizedPath && (status.startsWith('D') || status.includes('D'))) {
        deleted.add(normalizedPath);
      }
    }

    return { staged: new Set(), unstaged: new Set(), deleted };
  }

  function entryMatchesPrefix(relPosix, baseName) {
    if (!prefixRaw) return true;
    if (relPosix.startsWith(prefixRaw)) return true;
    if (baseName.startsWith(prefixRaw)) return true;
    const noTrail = prefixRaw.endsWith('/') ? prefixRaw.slice(0, -1) : prefixRaw;
    if (noTrail && (baseName === noTrail || relPosix === noTrail)) return true;
    return false;
  }

  // Get deleted files for this workdir
  const { deleted: deletedInner } = gitStatusPathSets(work);

  const rows = [];
  for (const ent of dirents) {
    if (ent.name === '.git') continue;
    const relPosix = dirRel ? `${dirRel}/${ent.name}` : ent.name;
    if (!entryMatchesPrefix(relPosix, ent.name)) continue;

    let isDir = ent.isDirectory();
    if (ent.isSymbolicLink()) {
      try {
        const st = fs.statSync(path.join(absDir, ent.name));
        isDir = st.isDirectory();
      } catch {
        continue;
      }
    }

    // Skip if this file is marked as deleted in git
    if (!isDir && deletedInner.has(normalizeRel(relPosix))) continue;

    let size = 0;
    if (!isDir) {
      try {
        size = fs.statSync(path.join(absDir, ent.name)).size;
      } catch {
        /* ignore */
      }
    }
    rows.push({
      type: isDir ? 'dir' : 'file',
      path: relPosix,
      size,
    });
  }

  rows.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
    return String(a.path).localeCompare(String(b.path));
  });

  const total = rows.length;
  const page = rows.slice(offset, offset + limit);
  const truncated = offset + page.length < total;
  res.json({
    entries: page,
    total,
    next_offset: offset + page.length,
    truncated,
  });
});

api.get('/layers/:layer_id/diff/parent/files', (req, res) => {
  res.json(getLayerParentDiffFiles(req.params.layer_id));
});

api.get('/layers/:layer_id/diff/parent/file', (req, res) => {
  const relPath = (req.query.path ?? '').toString();
  const out = getLayerParentUnifiedDiff(req.params.layer_id, relPath);
  if (!out.ok) return res.status(out.status).json(out.body);
  res.json(out.body);
});

api.get('/layers/:layer_id/git/commit/latest-log', async (req, res) => {
  const work = layerPrimaryGitWorkdir(req.params.layer_id);
  if (!work) return res.status(400).json({ detail: 'no git' });
  try {
    const t = await gitExec(['log', '-1', '--stat'], work);
    res.json({ log: t });
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

/** 与 Django ``forward_container_layer_git_log`` 及文件树侧栏一致：``text``、可选空列表 ``commits``。 */
api.get('/layers/:layer_id/git/log', async (req, res) => {
  const layerId = String(req.params.layer_id || '');
  let limit = 20;
  if (req.query.limit != null && String(req.query.limit).trim() !== '') {
    const n = parseInt(String(req.query.limit), 10);
    if (Number.isNaN(n)) return res.status(400).json({ detail: 'limit 必须为整数' });
    limit = Math.max(1, Math.min(100, n));
  }
  const rawPath = (req.query.path ?? '').toString().trim();
  const ctx = resolveLayerGitLogContext(layerId, rawPath);
  if (!ctx) {
    if (!layerPrimaryGitWorkdir(layerId)) return res.status(400).json({ detail: 'no git' });
    return res.status(400).json({ detail: 'path 不合法' });
  }
  const { work, pathspec } = ctx;
  const args = [
    'log',
    `-${limit}`,
    '--date=short',
    '--pretty=format:%h %ad %s',
  ];
  if (pathspec) args.push('--', pathspec);
  try {
    const t = (await gitExec(args, work)).replace(/\s+$/, '');
    if (!t) {
      return res.json({ text: '', commits: [] });
    }
    return res.json({ text: t, commits: [] });
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.post('/layers/:layer_id/git/add', async (req, res) => {
  const layerId = String(req.params.layer_id || '');
  const rawPath = (req.body?.path ?? '').toString().trim();
  if (!rawPath) return res.status(400).json({ detail: 'path 必填' });
  if (!layerPrimaryGitWorkdir(layerId)) return res.status(400).json({ detail: 'no git' });
  const ctx = resolveLayerGitLogContext(layerId, rawPath);
  if (!ctx) return res.status(400).json({ detail: 'path 不合法' });
  const { work, pathspec } = ctx;
  try {
    if (!pathspec) {
      await gitExec(['add', '.'], work);
    } else {
      const rel = safeRepoRelativePathForGitAdd(work, pathspec);
      if (!rel) return res.status(400).json({ detail: 'path 不合法' });
      await gitExec(['add', '--', rel], work);
    }
    let suggested_commit_message = '';
    try {
      suggested_commit_message = await suggestStagedCommitMessage(gitExec, work);
    } catch (e) {
      console.error('[onlineServiceJS] suggestStagedCommitMessage:', e);
    }
    res.json({
      ok: true,
      ...(suggested_commit_message ? { suggested_commit_message } : {}),
    });
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.post('/layers/:layer_id/git/unstage', async (req, res) => {
  const layerId = String(req.params.layer_id || '');
  const rawPath = (req.body?.path ?? '').toString().trim();
  if (!rawPath) return res.status(400).json({ detail: 'path 必填' });
  if (!layerPrimaryGitWorkdir(layerId)) return res.status(400).json({ detail: 'no git' });
  const ctx = resolveLayerGitLogContext(layerId, rawPath);
  if (!ctx) return res.status(400).json({ detail: 'path 不合法' });
  const { work, pathspec } = ctx;
  try {
    if (!pathspec) {
      await gitExec(['reset', 'HEAD', '.'], work);
    } else {
      const rel = safeRepoRelativePathForGitAdd(work, pathspec);
      if (!rel) return res.status(400).json({ detail: 'path 不合法' });
      await gitExec(['reset', 'HEAD', '--', rel], work);
    }
    res.json({ ok: true });
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.post('/layers/:layer_id/git/commit', async (req, res) => {
  const work = layerPrimaryGitWorkdir(req.params.layer_id);
  if (!work) return res.status(400).json({ detail: 'no git' });
  const msg = (req.body?.message || 'commit').toString();
  const sa = req.body?.stage_all;
  const doStageAll = sa === undefined || sa === true;
  try {
    if (doStageAll) {
      await gitExec(['add', '-A'], work);
    }
    await gitExec(['commit', '-m', msg], work);
    res.json({ ok: true });
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

api.post('/layers/:layer_id/git/push', async (req, res) => {
  const work = layerPrimaryGitWorkdir(req.params.layer_id);
  if (!work) return res.status(400).json({ detail: 'no git' });
  const pem = String(req.body?.ephemeral_ssh_private_key || '').trim();
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
  let keyPath = null;
  try {
    if (pem) {
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'push_'));
      keyPath = path.join(dir, 'k');
      let c = pem;
      if (!c.endsWith('\n')) c += '\n';
      fs.writeFileSync(keyPath, c, { mode: 0o600 });
      env.GIT_SSH_COMMAND = `ssh -i ${keyPath} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new`;
    }
    const branch = (req.body?.target_branch || '').toString().trim();
    const args = ['push'];
    // `git push origin <name>` 要求本地存在同名的 *本地 ref*。任务里传入的 `target_branch` 往往是
    // 要在远端建立的工作分支名，而 clone 后所在分支可能是 main，并无该本地分支，会报
    // "src refspec does not match any"。用 HEAD:<dst> 将当前工作区提交推送到远端分支。
    if (branch) {
      const dst = branch.startsWith('refs/') ? branch : `refs/heads/${branch}`;
      args.push('origin', `HEAD:${dst}`);
    } else {
      args.push('origin', 'HEAD');
    }
    await gitExec(args, work, env);
    const pushedRef = branch
      ? branch.startsWith('refs/')
        ? branch
        : `refs/heads/${branch}`
      : 'origin HEAD';
    console.log('[LayerGitPush] ok layer_id=%s ref=%s', req.params.layer_id, pushedRef);
    res.json({ ok: true });
  } catch (e) {
    console.warn('[LayerGitPush] fail layer_id=%s err=%s', req.params.layer_id, String(e.message || e));
    res.status(400).json({ detail: String(e.message || e) });
  } finally {
    if (keyPath) {
      try {
        fs.rmSync(path.dirname(keyPath), { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
  }
});

function findParentWorkdirForChildPrefix(rootsP, relPrefix) {
  const key = relPrefix || '';
  const hit = rootsP.find((x) => (x.relPrefix || '') === key);
  if (hit) return hit.workdir;
  if (rootsP.length === 1 && !rootsP[0].relPrefix) return rootsP[0].workdir;
  return null;
}

const AI_SUMMARY_MAX_DIFF = 28000;
const AI_SUMMARY_TIMEOUT_MS = 45000;

function outboundLog(line) {
  try {
    const f = path.join(reqLogsDir(), 'outbound.log');
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.appendFileSync(f, `${new Date().toISOString()} | ${line}\n`);
  } catch {
    /* ignore */
  }
}

function resolveLlmFromEnv() {
  const baseUrl = String(process.env.TRAE_STAGED_COMMIT_LLM_BASE_URL || '')
    .trim()
    .replace(/\/$/, '');
  const apiKey = String(process.env.TRAE_STAGED_COMMIT_LLM_API_KEY || '').trim();
  const model = String(process.env.TRAE_STAGED_COMMIT_LLM_MODEL || '').trim();
  if (baseUrl && apiKey && model) return { baseUrl, apiKey, model };
  return null;
}

function resolveLlmFromYaml() {
  const p = configFilePath();
  if (!fs.existsSync(p)) return null;
  let doc;
  try {
    doc = YAML.parse(fs.readFileSync(p, 'utf8'));
  } catch {
    return null;
  }
  if (!doc || typeof doc !== 'object') return null;
  const agentKey = doc.agents?.trae_agent?.model;
  if (!agentKey || typeof agentKey !== 'string') return null;
  const mdef = doc.models?.[agentKey];
  if (!mdef || typeof mdef !== 'object') return null;
  const provKey = mdef.model_provider;
  const modelId = mdef.model;
  if (!provKey || !modelId) return null;
  const prov = doc.model_providers?.[provKey];
  if (!prov || typeof prov !== 'object') return null;
  const apiKey = String(prov.api_key || '').trim();
  if (!apiKey || apiKey.includes('your_')) return null;
  let baseUrl = String(prov.base_url || '').trim().replace(/\/$/, '');
  const provName = String(prov.provider || provKey || '').toLowerCase();
  if (!baseUrl) {
    if (provName === 'openai') baseUrl = 'https://api.openai.com/v1';
    else if (provName === 'openrouter') baseUrl = 'https://openrouter.ai/api/v1';
    else return null;
  }
  return { baseUrl, apiKey, model: String(modelId) };
}

async function callOpenAiCompatibleChat({ baseUrl, apiKey, model }, userContent) {
  const url = `${baseUrl}/chat/completions`;
  outboundLog(`diff-log-summary POST ${url} model=${model}`);
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), AI_SUMMARY_TIMEOUT_MS);
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        messages: [
          {
            role: 'system',
            content: '你是一个代码变更总结助手。请根据用户提供的 git diff 内容，用简洁的中文总结用户做了什么修改。输出格式：1. 变更类型：描述；2. 涉及文件：文件名列表；3. 主要改动：简要说明。保持简洁明了。',
          },
          { role: 'user', content: userContent },
        ],
        max_tokens: 256,
        temperature: 0.3,
      }),
      signal: ac.signal,
    });
    const text = await r.text();
    if (!r.ok) {
      outboundLog(`diff-log-summary LLM HTTP ${r.status} ${text.slice(0, 240)}`);
      return null;
    }
    let j;
    try {
      j = JSON.parse(text);
    } catch {
      return null;
    }
    const c = j?.choices?.[0]?.message?.content;
    return typeof c === 'string' ? c.trim() : null;
  } catch (e) {
    outboundLog(`diff-log-summary LLM error ${String(e?.message || e).slice(0, 320)}`);
    return null;
  } finally {
    clearTimeout(t);
  }
}

function heuristicSummary(diffLogs) {
  const changed = diffLogs.filter(d => d.has_changes);
  const removed = changed.filter(d => d.diff.includes('/dev/null')).map(d => d.file);
  const added = changed.filter(d => d.diff.startsWith('--- /dev/null')).map(d => d.file);
  const modified = changed.filter(d => !d.diff.includes('/dev/null') || !d.diff.startsWith('--- /dev/null')).map(d => d.file);

  const parts = [];
  if (removed.length > 0) {
    parts.push(`删除文件：${removed.join(', ')}`);
  }
  if (added.length > 0) {
    parts.push(`新增文件：${added.join(', ')}`);
  }
  if (modified.length > 0) {
    parts.push(`修改文件：${modified.join(', ')}`);
  }
  if (parts.length === 0) {
    return '未检测到变更';
  }
  return parts.join('；');
}

async function generateDiffSummary(diffLogs) {
  if (String(process.env.TRAE_STAGED_COMMIT_LLM_DISABLE || '').trim() === '1') {
    return heuristicSummary(diffLogs);
  }

  const changed = diffLogs.filter(d => d.has_changes);
  if (changed.length === 0) {
    return '未检测到变更';
  }

  const diffContent = changed.map(d => `=== ${d.file} ===\n${d.diff}`).join('\n\n');
  const diffTrim = diffContent.slice(0, AI_SUMMARY_MAX_DIFF);

  const creds = resolveLlmFromEnv() || resolveLlmFromYaml();
  if (creds && diffTrim.trim()) {
    const summary = await callOpenAiCompatibleChat(
      creds,
      `以下是 git diff 内容（可能被截断）：\n\n${diffTrim}`,
    );
    if (summary) return summary;
  }

  return heuristicSummary(diffLogs);
}

api.post('/layers/:layer_id/git/diff-log', async (req, res) => {
  const lid = req.params.layer_id;
  const rootsC = layerGitWorkdirRootsForFileListing(lid);
  const meta = readLayerMeta(lid);
  const known = new Set(listLayerRows().map((r) => r.layer_id));
  let parentId = meta?.parent_layer_id && known.has(meta.parent_layer_id) ? meta.parent_layer_id : null;
  if (!parentId) parentId = resolvedParentLayerId(lid, known, null);
  const rootsP = parentId ? layerGitWorkdirRootsForFileListing(parentId) : [];

  const files = Array.isArray(req.body?.files) ? req.body.files : [];
  if (!files.length) {
    return res.status(400).json({ detail: 'files array required' });
  }

  const sanitizedFiles = files
    .map(f => String(f || '').trim())
    .filter(f => f && !f.includes('..') && !f.startsWith('/'));

  if (!sanitizedFiles.length) {
    return res.status(400).json({ detail: 'no valid files provided' });
  }

  const diffLogs = [];
  for (const filePath of sanitizedFiles) {
    try {
      let diff = '';
      let hasChanges = false;

      const norm = filePath.replace(/\\/g, '/');
      const segs = norm ? norm.split('/').filter((x) => x.length) : [];

      let workdirC = null;
      let workdirP = null;
      let innerPath = null;

      for (const rootC of rootsC) {
        if (!rootC.relPrefix) {
          workdirC = rootC.workdir;
          innerPath = filePath;
          const rootP = findParentWorkdirForChildPrefix(rootsP, rootC.relPrefix);
          if (rootP) workdirP = rootP;
          break;
        }
        if (segs[0] === rootC.relPrefix) {
          workdirC = rootC.workdir;
          innerPath = segs.slice(1).join('/');
          const rootP = findParentWorkdirForChildPrefix(rootsP, rootC.relPrefix);
          if (rootP) workdirP = rootP;
          break;
        }
      }

      if (!workdirC) {
        workdirC = layerPrimaryGitWorkdir(lid);
        innerPath = filePath;
        if (parentId) workdirP = layerPrimaryGitWorkdir(parentId);
      }

      if (!workdirC) {
        diffLogs.push({ file: filePath, diff: '', has_changes: false, error: 'no git workdir found' });
        continue;
      }

      try {
        diff = await gitExec(['diff', 'HEAD', '--', innerPath], workdirC);
        hasChanges = diff.trim().length > 0;
      } catch (_) {}

      if (!hasChanges) {
        try {
          const cachedDiff = await gitExec(['diff', '--cached', 'HEAD', '--', innerPath], workdirC);
          if (cachedDiff.trim().length > 0) {
            diff = cachedDiff;
            hasChanges = true;
          }
        } catch (_) {}
      }

      if (!hasChanges) {
        try {
          const statusOut = await gitExec(['status', '--porcelain', '--', innerPath], workdirC);
          const statusLines = statusOut.trim().split('\n').filter(Boolean);
          for (const line of statusLines) {
            const status = line.slice(0, 2).trim();
            if (status === 'D' || status === 'D ' || status === ' D' || status.includes('D')) {
              const showOut = await gitExec(['show', `HEAD:${innerPath}`], workdirC);
              diff = `--- a/${filePath}\n+++ /dev/null\n-${showOut.trim().split('\n').map(l => l || '\\ No newline at end of file').join('\n-')}`;
              hasChanges = true;
              break;
            }
          }
        } catch (_) {}
      }

      if (!hasChanges && workdirP) {
        try {
          const pathInCurrent = path.join(workdirC, innerPath);
          const pathInParent = path.join(workdirP, innerPath);

          const existsInCurrent = fs.existsSync(pathInCurrent);
          const existsInParent = fs.existsSync(pathInParent);

          if (!existsInCurrent && existsInParent) {
            const parentContent = fs.readFileSync(pathInParent, 'utf8');
            diff = `--- a/${filePath}\n+++ /dev/null\n-${parentContent.trim().split('\n').map(l => l).join('\n-')}`;
            hasChanges = true;
          } else if (existsInCurrent && !existsInParent) {
            const currentContent = fs.readFileSync(pathInCurrent, 'utf8');
            diff = `--- /dev/null\n+++ b/${filePath}\n+${currentContent.trim().split('\n').map(l => l).join('\n+')}`;
            hasChanges = true;
          } else if (existsInCurrent && existsInParent) {
            const parentContent = fs.readFileSync(pathInParent, 'utf8');
            const currentContent = fs.readFileSync(pathInCurrent, 'utf8');
            if (parentContent !== currentContent) {
              const parentLines = parentContent.trim().split('\n');
              const currentLines = currentContent.trim().split('\n');
              const parts = [];
              parts.push(`--- a/${filePath}`);
              parts.push(`+++ b/${filePath}`);
              const maxLines = Math.max(parentLines.length, currentLines.length);
              for (let i = 0; i < maxLines; i++) {
                const parentLine = parentLines[i] || '';
                const currentLine = currentLines[i] || '';
                if (parentLine !== currentLine) {
                  if (parentLine !== undefined) parts.push(`-${parentLine}`);
                  if (currentLine !== undefined) parts.push(`+${currentLine}`);
                } else {
                  parts.push(` ${parentLine}`);
                }
              }
              diff = parts.join('\n');
              hasChanges = true;
            }
          }
        } catch (e) {
          console.error('File system diff error:', e);
        }
      }

      diffLogs.push({
        file: filePath,
        diff: diff.trim(),
        has_changes: hasChanges,
      });
    } catch (e) {
      diffLogs.push({
        file: filePath,
        diff: '',
        has_changes: false,
        error: String(e.message || e),
      });
    }
  }

  const summary = await generateDiffSummary(diffLogs);

  const logContent = diffLogs
    .filter(d => d.has_changes)
    .map(d => `=== ${d.file} ===\n${d.diff}\n`)
    .join('\n');

  res.json({
    layer_id: req.params.layer_id,
    files: diffLogs,
    log: logContent,
    summary: summary,
    changed_files_count: diffLogs.filter(d => d.has_changes).length,
  });
});

api.get('/git/identity', (req, res) => {
  res.json({ name: '', email: '' });
});

api.post('/git/identity', (req, res) => {
  res.json({ ok: true });
});

api.get('/dev/service-repo-git-push', (req, res) => {
  res.json({
    is_git: false,
    ahead: 0,
    branch: '',
    upstream: '',
    no_upstream: true,
    path: repoRoot(),
  });
});

api.post('/project/view', (req, res) => {
  res.json({ status: 'ok', active_tip_layer_id: (req.body?.layer_id || '').toString() });
});

api.get('/project/active', (req, res) => {
  res.json({ active_tip_layer_id: bootstrapCloneLayerId, note: 'onlineServiceJS' });
});

app.use('/api', api);

const port = parseInt(process.env.PORT || '8765', 10);
const host = '0.0.0.0';

async function main() {
  ssePingLoop();
  const strict = ['1', 'true', 'yes', 'on'].includes(
    String(process.env.TASK_API_BOOTSTRAP_STRICT_STARTUP || '').toLowerCase()
  );
  let bootstrapCtx;
  try {
    bootstrapCtx = await runBootstrapTokenExchangeOnly();
  } catch (e) {
    console.error('[onlineServiceJS] bootstrap (token) error:', e);
    if (strict) process.exit(1);
    bootstrapCtx = { skipped: true };
  }
  ensureStartupEmptyLayer();
  try {
    sweepDanglingLayerDirs();
  } catch (e) {
    console.error('[onlineServiceJS] layer dir sweep error:', e);
  }

  await new Promise((resolve, reject) => {
    try {
      app.listen(port, host, async () => {
        console.log(`[onlineServiceJS] server listening on http://${host}:${port}`);
        broadcast({ type: 'service_ready', port });
        try {
          await runBootstrapAfterListen(bootstrapCtx);
          if (bootstrapCloneLayerId && bootstrapRegisterCloneJob) {
            registerBootstrapCloneJob(bootstrapCloneLayerId);
          }
        } catch (e) {
          console.error('[onlineServiceJS] bootstrap (post-listen) error:', e);
          if (strict) process.exit(1);
        }
        resolve();
      });
    } catch (e) {
      reject(e);
    }
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
