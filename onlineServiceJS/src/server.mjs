import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import express from 'express';
import multer from 'multer';
import YAML from 'yaml';
import { spawn } from 'child_process';

import { authMiddleware, accessTokenExpected } from './auth.mjs';
import { getAgentRenderHints } from './agentRenderHints.mjs';
import { serviceRoot, configFilePath, repoRoot, logsDir } from './paths.mjs';
import { ssePingLoop, addSseClient, broadcast } from './sseHub.mjs';
import {
  runBootstrap,
  bootstrapCloneLayerId,
  ensureStartupEmptyLayer,
  getCloneLayerLogText,
  clearCloneLayerLog,
  startupEmptyLayerId,
} from './bootstrap.mjs';
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
  deleteLayerTree,
  directChildLayerIds,
  repoDirNameFromUrl,
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
} from './jobsRuntime.mjs';
import { getJobStepsForLayer } from './jobSteps.mjs';
import { getLayerParentDiffFiles, getLayerParentUnifiedDiff } from './layerParentDiff.mjs';
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

function buildGitCloneArgs(cloneUrl, { branch, depth }) {
  const args = ['clone', '--progress'];
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
    const proc = spawn('git', args, { cwd, env: { ...process.env, ...env, GIT_TERMINAL_PROMPT: '0' } });
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
  const payload = getJobStepsForLayer(j.layer_id);
  res.json(payload);
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

api.post('/jobs/reset', (req, res) => {
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
  res.json({
    layer_id: lid,
    text: lid ? getCloneLayerLogText(lid) : '',
  });
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
    fs.mkdirSync(root, { recursive: true });
    // 与 Python git_clone 一致：克隆到 base/，layer_meta.json 在后台成功后写入层根
    const cloneCwd = path.join(root, 'base');
    fs.mkdirSync(cloneCwd, { recursive: true });
    clearCloneLayerLog(lid);

    let cloneUrl = url;
    const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
    if (usePem) {
      cloneUrl = url.toLowerCase().startsWith('https://') ? gitSshFromHttps(url) : url;
      const dir = fs.mkdtempSync(path.join(path.tmpdir(), 'ui_clone_'));
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
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
  let keyPath = null;
  let cloneUrl = repoUrl;
  try {
    if (usePem) {
      cloneUrl = repoUrl.toLowerCase().startsWith('https://') ? gitSshFromHttps(repoUrl) : repoUrl;
      const dir = fs.mkdtempSync(path.join(path.tmpdir(), 'reclone_'));
      keyPath = path.join(dir, 'k');
      let c = pem;
      if (!c.endsWith('\n')) c += '\n';
      fs.writeFileSync(keyPath, c, { mode: 0o600 });
      env.GIT_SSH_COMMAND = gitSshCommandFromIdentityFile(keyPath);
    }
    await gitExec(buildGitCloneArgs(cloneUrl, { branch: '', depth: null }), target, env);
    res.json({ layer_id: layerId, output: 'ok' });
  } catch (e) {
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

api.delete('/layers/:layer_id', (req, res) => {
  const lid = req.params.layer_id;
  for (const cid of directChildLayerIds(lid)) {
    deleteLayerTree(cid);
  }
  deleteLayerTree(lid);
  res.json({ ok: true });
});

api.get('/layers/:layer_id/files', (req, res) => {
  const work = layerPrimaryGitWorkdir(req.params.layer_id);
  if (!work) return res.json({ files: [] });
  const files = [];
  function walk(d, rel) {
    for (const ent of fs.readdirSync(d, { withFileTypes: true })) {
      if (ent.name === '.git') continue;
      const p = path.join(d, ent.name);
      const r = rel ? `${rel}/${ent.name}` : ent.name;
      if (ent.isDirectory()) walk(p, r);
      else files.push(r);
      if (files.length >= 2000) return;
    }
  }
  try {
    walk(work, '');
  } catch {
    /* ignore */
  }
  res.json({ files });
});

api.get('/layers/:layer_id/files/*', (req, res) => {
  const lid = req.params.layer_id;
  const rel = req.params[0] || '';
  const work = layerPrimaryGitWorkdir(lid);
  if (!work) return res.status(404).json({ detail: 'not found' });
  const fp = path.resolve(path.join(work, rel));
  if (!fp.startsWith(path.resolve(work))) return res.status(400).json({ detail: 'invalid path' });
  if (!fs.existsSync(fp) || !fs.statSync(fp).isFile()) return res.status(404).json({ detail: 'not found' });
  const max = Math.min(parseInt(req.query.max_bytes || '2000000', 10) || 2000000, 20_000_000);
  const buf = fs.readFileSync(fp).subarray(0, max);
  const text = buf.toString('utf8');
  res.json({ path: rel, content: text, truncated: buf.length >= max });
});

api.get('/layers/:layer_id/children', (req, res) => {
  const work = layerPrimaryGitWorkdir(req.params.layer_id);
  if (!work) return res.json({ entries: [] });
  const dir = path.join(work, (req.query.dir || '.').toString());
  const entries = [];
  try {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      entries.push({ name: ent.name, is_dir: ent.isDirectory() });
    }
  } catch {
    /* ignore */
  }
  res.json({ entries });
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

api.post('/layers/:layer_id/git/commit', async (req, res) => {
  const work = layerPrimaryGitWorkdir(req.params.layer_id);
  if (!work) return res.status(400).json({ detail: 'no git' });
  const msg = (req.body?.message || 'commit').toString();
  try {
    await gitExec(['add', '-A'], work);
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
      const dir = fs.mkdtempSync(path.join(path.tmpdir(), 'push_'));
      keyPath = path.join(dir, 'k');
      let c = pem;
      if (!c.endsWith('\n')) c += '\n';
      fs.writeFileSync(keyPath, c, { mode: 0o600 });
      env.GIT_SSH_COMMAND = `ssh -i ${keyPath} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new`;
    }
    const branch = (req.body?.target_branch || '').toString().trim();
    const args = ['push'];
    if (branch) args.push('origin', branch);
    else args.push('origin', 'HEAD');
    await gitExec(args, work, env);
    res.json({ ok: true });
  } catch (e) {
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
  try {
    await runBootstrap();
    if (bootstrapCloneLayerId) {
      registerBootstrapCloneJob(bootstrapCloneLayerId);
    }
  } catch (e) {
    console.error('[onlineServiceJS] bootstrap error:', e);
    if (strict) process.exit(1);
  }
  ensureStartupEmptyLayer();

  app.listen(port, host, () => {
    console.log(`[onlineServiceJS] server listening on http://${host}:${port}`);
    broadcast({ type: 'service_ready', port });
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
