import fs from 'fs';
import { spawn } from 'child_process';
import path from 'path';
import crypto from 'crypto';

import { bootstrapCloneLayerId } from './bootstrap.mjs';
import { jobsStatePath, configFilePath, repoRoot, layersRoot } from './paths.mjs';
import {
  createStackedLayer,
  directChildLayerIds,
  deleteLayerTree,
  layerPath,
  layerPrimaryGitWorkdir,
  anyLayerHasGitRepo,
  listLayerRows,
  readLayerMeta,
  resolvedParentLayerId,
  newLayerId,
  gitWorktreeDirty,
} from './layerFs.mjs';
import { broadcast } from './sseHub.mjs';
import { resetExecStream, appendExecStream, completeExecStream } from './execStream.mjs';

/** @type {Map<string, object>} */
const jobs = new Map();
/** @type {Map<string, import('child_process').ChildProcess>} */
const running = new Map();

function newJobId() {
  return crypto.randomUUID();
}

function saveState() {
  const payload = {
    jobs: [...jobs.values()].map((j) => ({ ...j })),
    layer_queues: {},
  };
  const p = jobsStatePath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(payload, null, 2), 'utf8');
}

function loadState() {
  const p = jobsStatePath();
  if (!fs.existsSync(p)) return;
  try {
    const data = JSON.parse(fs.readFileSync(p, 'utf8'));
    for (const row of data.jobs || []) {
      if (!row.id) continue;
      if (row.status === 'running') row.status = 'interrupted';
      jobs.set(row.id, row);
    }
  } catch {
    /* ignore */
  }
}

function venvTraePaths() {
  const venv = String(process.env.TRAE_VENV || path.join(repoRoot(), '.venv')).trim();
  return {
    traeCli: path.join(venv, 'bin', 'trae-cli'),
    py: path.join(venv, 'bin', 'python'),
    py3: path.join(venv, 'bin', 'python3'),
  };
}

function buildTraeCmd(workDir, cmdText) {
  const custom = String(process.env.TRAE_CLI || '').trim();
  if (custom) {
    return { cmd: custom, args: [cmdText, `--working-dir=${workDir}`], shell: true };
  }
  const { traeCli, py, py3 } = venvTraePaths();
  const cfg = configFilePath();
  if (fs.existsSync(traeCli)) {
    return { cmd: traeCli, args: ['run', cmdText, `--config-file=${cfg}`, `--working-dir=${workDir}`], shell: false };
  }
  if (fs.existsSync(py)) {
    return {
      cmd: py,
      args: ['-m', 'trae_agent.cli', 'run', cmdText, `--config-file=${cfg}`, `--working-dir=${workDir}`],
      shell: false,
    };
  }
  if (fs.existsSync(py3)) {
    return {
      cmd: py3,
      args: ['-m', 'trae_agent.cli', 'run', cmdText, `--config-file=${cfg}`, `--working-dir=${workDir}`],
      shell: false,
    };
  }
  return null;
}

export function jobToApiDict(rec) {
  return { ...rec, git_destructive_locked: false };
}

export function listJobs() {
  return [...jobs.values()].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
}

export function getJob(id) {
  return jobs.get(id) || null;
}

function removeJobsForLayer(layerId) {
  for (const [jid, j] of jobs) {
    if (j.layer_id === layerId) jobs.delete(jid);
  }
}

function purgeChildLayers(baseLayerId) {
  for (const lid of directChildLayerIds(baseLayerId)) {
    try {
      removeJobsForLayer(lid);
      deleteLayerTree(lid);
    } catch {
      /* ignore */
    }
  }
}

function createdAtMsForSort(iso) {
  const m = Date.parse(iso || '');
  return Number.isFinite(m) ? m : 0;
}

/**
 * 与 static/index.html sortLayersSerialChronological 一致：created_at 旧→新，同秒 bootstrap 层优先。
 */
function sortLayersSerialChronological(layerList, bootstrapLayerId) {
  const bs = String(bootstrapLayerId || '').trim();
  const bsUse = bs && layerList.some((r) => r.layer_id === bs) ? bs : '';
  return [...layerList].sort((a, b) => {
    const da = createdAtMsForSort(a.created_at);
    const db = createdAtMsForSort(b.created_at);
    if (da !== db) return da - db;
    const sa = bsUse && a.layer_id === bsUse ? 1 : 0;
    const sb = bsUse && b.layer_id === bsUse ? 1 : 0;
    if (sa !== sb) return sb - sa;
    return String(a.layer_id || '').localeCompare(String(b.layer_id || ''));
  });
}

function obliterateLayer(layerId) {
  const lid = String(layerId || '').trim();
  if (!lid) return;
  const ids = [...jobs.entries()].filter(([, j]) => j.layer_id === lid).map(([id]) => id);
  for (const jid of ids) {
    try {
      deleteJob(jid);
    } catch {
      /* job 可能已删 */
    }
  }
  if (fs.existsSync(layerPath(lid))) {
    try {
      deleteLayerTree(lid);
    } catch {
      /* ignore */
    }
  }
  saveState();
}

/**
 * 串行列表中锚点层之后的所有可写层（更新者）删除，与页面点选某层再「创建并执行」一致。
 */
function purgeSerialTailAfterLayer(anchorLayerId) {
  const anchor = String(anchorLayerId || '').trim();
  if (!anchor) return;
  const snap = buildLayersSnapshot(bootstrapCloneLayerId);
  const sorted = sortLayersSerialChronological(snap.layers, snap.bootstrap_layer_id);
  const idx = sorted.findIndex((x) => x.layer_id === anchor);
  if (idx < 0) return;
  const tail = sorted.slice(idx + 1);
  for (let i = tail.length - 1; i >= 0; i--) {
    obliterateLayer(tail[i].layer_id);
  }
}

export async function createJob(body) {
  const command = String(body.command || '').trim();
  if (!command) throw new Error('command 不能为空');
  const command_kind = (body.command_kind || 'trae').toLowerCase();
  if (!['trae', 'shell'].includes(command_kind)) throw new Error('invalid command_kind');
  const parent_job_id = body.parent_job_id ? String(body.parent_job_id).trim() : '';
  const repo_layer_id = body.repo_layer_id ? String(body.repo_layer_id).trim() : '';
  if (Boolean(parent_job_id) === Boolean(repo_layer_id)) {
    throw new Error('须且仅能设置 parent_job_id 或 repo_layer_id 之一');
  }
  if (!anyLayerHasGitRepo()) throw new Error('请先完成「克隆仓库」后再创建任务。');

  if (command_kind === 'trae' && !fs.existsSync(configFilePath())) {
    throw new Error(`Config missing: ${configFilePath()}`);
  }

  let stackParent;
  if (parent_job_id) {
    const p = jobs.get(parent_job_id);
    if (!p) throw new Error(`parent_job_id not found: ${parent_job_id}`);
    stackParent = p.layer_id;
  } else {
    if (!fs.existsSync(layerPath(repo_layer_id))) throw new Error(`repo_layer_id not found: ${repo_layer_id}`);
    stackParent = repo_layer_id;
  }

  purgeSerialTailAfterLayer(stackParent);
  purgeChildLayers(stackParent);

  const lid = newLayerId();
  createStackedLayer(lid, stackParent);
  const lp = layerPath(lid);
  const work = layerPrimaryGitWorkdir(lid) || lp;

  const id = newJobId();
  const rec = {
    id,
    layer_id: lid,
    layer_path: lp,
    command,
    parent_job_id: parent_job_id || null,
    repo_layer_id: repo_layer_id || null,
    status: 'pending',
    created_at: new Date().toISOString(),
    exit_code: null,
    output: '',
    git_branch: body.git_branch || null,
    git_head_at_run_start: null,
    command_kind,
    command_env: body.env && typeof body.env === 'object' ? body.env : null,
  };
  jobs.set(id, rec);
  saveState();
  broadcast({ type: 'job_created', job_id: id, layer_id: lid });
  runJobAsync(rec, work);
  return rec;
}

function runJobAsync(rec, workDir) {
  resetExecStream('job', rec.id);
  const env = { ...process.env, PYTHONUNBUFFERED: '1' };
  if (rec.command_env && typeof rec.command_env === 'object') {
    for (const [k, v] of Object.entries(rec.command_env)) {
      if (v != null) env[String(k)] = String(v);
    }
  }
  let proc;
  if (rec.command_kind === 'shell') {
    proc = spawn('bash', ['-lc', rec.command], { cwd: workDir, env });
  } else {
    const trae = buildTraeCmd(workDir, rec.command);
    if (trae) {
      proc = spawn(trae.cmd, trae.args, { cwd: workDir, env, shell: trae.shell || false });
    } else {
      proc = spawn(
        'bash',
        [
          '-lc',
          `echo "[onlineServiceJS] trae stub (no .venv trae-cli): ${rec.command.replace(/'/g, "'\\''")}"; exit 0`,
        ],
        { cwd: workDir, env }
      );
    }
  }
  try {
    proc.stdout?.on('data', (c) => {
      const t = c.toString();
      rec.output = (rec.output || '') + t;
      appendExecStream('job', rec.id, t);
    });
    proc.stderr?.on('data', (c) => {
      const t = c.toString();
      rec.output = (rec.output || '') + t;
      appendExecStream('job', rec.id, t);
    });
  } catch {
    /* ignore */
  }
  rec.status = 'running';
  running.set(rec.id, proc);
  saveState();
  broadcast({ type: 'job_started', job_id: rec.id });
  proc.on('close', (code) => {
    running.delete(rec.id);
    rec.exit_code = code;
    rec.status = code === 0 ? 'completed' : 'failed';
    completeExecStream('job', rec.id);
    saveState();
    broadcast({ type: 'job_finished', job_id: rec.id, status: rec.status, exit_code: code });
  });
  proc.on('error', (e) => {
    running.delete(rec.id);
    rec.status = 'failed';
    rec.exit_code = -1;
    rec.output = (rec.output || '') + `\n[error] ${e.message}\n`;
    appendExecStream('job', rec.id, `\n[error] ${e.message}\n`);
    completeExecStream('job', rec.id);
    saveState();
    broadcast({ type: 'job_finished', job_id: rec.id, status: 'failed' });
  });
}

export function interruptJob(jobId) {
  const rec = jobs.get(jobId);
  if (!rec) throw new Error('job not found');
  const proc = running.get(jobId);
  if (proc && !proc.killed) {
    proc.kill('SIGTERM');
  }
  if (rec.status === 'running') rec.status = 'interrupted';
  saveState();
  return rec;
}

export function deleteJob(jobId) {
  const rec = jobs.get(jobId);
  if (!rec) throw new Error('job not found');
  interruptJob(jobId);
  deleteLayerTree(rec.layer_id);
  jobs.delete(jobId);
  saveState();
  return { ok: true };
}

export function registerBootstrapCloneJob(layerId) {
  const id = newJobId();
  const rec = {
    id,
    layer_id: layerId,
    layer_path: layerPath(layerId),
    command: '[bootstrap] 容器引导克隆',
    parent_job_id: null,
    repo_layer_id: null,
    status: 'completed',
    created_at: new Date().toISOString(),
    exit_code: 0,
    output: '',
    git_branch: null,
    git_head_at_run_start: null,
    command_kind: 'clone',
    command_env: null,
  };
  jobs.set(id, rec);
  saveState();
}

export function buildLayersSnapshot(bootstrapLayerId) {
  const rows = listLayerRows();
  const known = new Set(rows.map((r) => r.layer_id));
  const jobsList = listJobs();
  const cmdByLayer = {};
  const cloneCmdByLayer = {};
  for (let i = jobsList.length - 1; i >= 0; i--) {
    const j = jobsList[i];
    const lid = String(j.layer_id || '').trim();
    if (!lid) continue;
    if (j.command_kind === 'clone') {
      if (cloneCmdByLayer[lid] === undefined && j.command) cloneCmdByLayer[lid] = j.command;
      continue;
    }
    if (cmdByLayer[lid] === undefined) cmdByLayer[lid] = j.command;
  }
  const jobByLayer = {};
  for (let i = jobsList.length - 1; i >= 0; i--) {
    const j = jobsList[i];
    const lid = String(j.layer_id || '').trim();
    if (!lid || j.command_kind === 'clone') continue;
    if (!jobByLayer[lid]) jobByLayer[lid] = j;
  }
  const layers = [];
  for (const row of rows) {
    const lid = row.layer_id;
    const meta = readLayerMeta(lid);
    if (meta?.kind === 'empty') continue;
    let displayCmd = cmdByLayer[lid] || null;
    if (!displayCmd && meta?.kind === 'clone' && meta.clone_url) {
      displayCmd = `git clone ${meta.clone_url}`;
    }
    if (!displayCmd && cloneCmdByLayer[lid]) {
      displayCmd = cloneCmdByLayer[lid];
    }
    const item = {
      layer_id: lid,
      created_at: row.created_at,
      command: displayCmd,
      parent_layer_id: resolvedParentLayerId(lid, known, jobsList),
      job_id: jobByLayer[lid]?.id || null,
      job_status: jobByLayer[lid]?.status || null,
      queue_depth: 0,
      mind_state: jobByLayer[lid]?.status === 'running' || jobByLayer[lid]?.status === 'pending' ? 'running' : 'idle_done',
      git_worktree_dirty: gitWorktreeDirty(lid),
      git_remote: {},
      meta_kind: meta?.kind || 'clone',
    };
    layers.push(item);
  }
  const bs = String(bootstrapLayerId || '').trim();
  if (bs) {
    const idx = layers.findIndex((x) => x.layer_id === bs);
    if (idx > 0) {
      const [sp] = layers.splice(idx, 1);
      layers.unshift(sp);
    }
  }
  return {
    layers,
    jobs: jobsList.map(jobToApiDict),
    layers_root: layersRoot(),
    bootstrap_layer_id: bs || null,
  };
}

loadState();
