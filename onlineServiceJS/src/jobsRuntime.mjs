import fs from 'fs';
import { spawn } from 'child_process';
import path from 'path';
import crypto from 'crypto';

import { bootstrapCloneLayerId } from './bootstrap.mjs';
import {
  jobsStatePath,
  configFilePath,
  repoRoot,
  layersRoot,
  layerArtifactsDir,
  jobLogsTaeJsonDir,
  jobLogsTaeJsonPath,
} from './paths.mjs';
import { getCloneOpStatus } from './cloneQueue.mjs';
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
  layerRootOrChildHasGit,
  layerGitRemoteSnapshot,
} from './layerFs.mjs';
import { broadcast } from './sseHub.mjs';
import { resetExecStream, appendExecStream, completeExecStream } from './execStream.mjs';
import { publishLayerGraphSnapshotToSaas } from './saasTaskCloud.mjs';

/** @type {Map<string, object>} */
const jobs = new Map();
/** @type {Map<string, import('child_process').ChildProcess>} */
const running = new Map();

/** @type {Map<string, Array<{ phase: string, message: string, ts: number }>>} */
const jobEvents = new Map();

/** 某层上「当前任务结束后」按顺序执行的指令（与 UI 加入队列一致）；键为层 id，值为待执行项 */
/** @type {Record<string, Array<{ command: string, command_kind: string, env?: object | null }>>} */
let layerQueues = {};

function recordJobEvent(jobId, phase, message = '') {
  const events = jobEvents.get(jobId) || [];
  events.push({ phase, message, ts: Date.now() });
  jobEvents.set(jobId, events);
}

export function getJobEvents(jobId, offset = 0, limit = 500) {
  const events = jobEvents.get(jobId) || [];
  const start = Math.max(0, offset);
  const end = start + limit;
  return {
    events: events.slice(start, end),
    next_offset: end < events.length ? end : null,
  };
}

function newJobId() {
  return crypto.randomUUID();
}

function saveState() {
  const payload = {
    jobs: [...jobs.values()].map((j) => ({ ...j })),
    layer_queues: { ...layerQueues },
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
    const lq = data.layer_queues;
    if (lq && typeof lq === 'object' && !Array.isArray(lq)) {
      layerQueues = {};
      for (const [k, v] of Object.entries(lq)) {
        if (!k || !Array.isArray(v)) continue;
        const cleaned = v
          .filter((x) => x && String(x.command || '').trim())
          .map((x) => ({
            command: String(x.command).trim(),
            command_kind: String(x.command_kind || 'trae').toLowerCase(),
            env: x.env && typeof x.env === 'object' ? x.env : null,
          }));
        if (cleaned.length) layerQueues[k] = cleaned;
      }
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

function buildTraeCmd(workDir, cmdText, opts = {}) {
  const { trajectoryFile } = opts;
  const custom = String(process.env.TRAE_CLI || '').trim();
  if (custom) {
    const args = [cmdText, `--working-dir=${workDir}`];
    if (trajectoryFile) args.push(`--trajectory-file=${trajectoryFile}`);
    return { cmd: custom, args, shell: true };
  }
  const { traeCli, py, py3 } = venvTraePaths();
  const cfg = configFilePath();
  if (fs.existsSync(traeCli)) {
    const a = ['run', cmdText, `--config-file=${cfg}`, `--working-dir=${workDir}`];
    if (trajectoryFile) a.push(`--trajectory-file=${trajectoryFile}`);
    return { cmd: traeCli, args: a, shell: false };
  }
  if (fs.existsSync(py)) {
    return {
      cmd: py,
      args: [
        '-m',
        'trae_agent.cli',
        'run',
        cmdText,
        `--config-file=${cfg}`,
        `--working-dir=${workDir}`,
        ...(trajectoryFile ? [`--trajectory-file=${trajectoryFile}`] : []),
      ],
      shell: false,
    };
  }
  if (fs.existsSync(py3)) {
    return {
      cmd: py3,
      args: [
        '-m',
        'trae_agent.cli',
        'run',
        cmdText,
        `--config-file=${cfg}`,
        `--working-dir=${workDir}`,
        ...(trajectoryFile ? [`--trajectory-file=${trajectoryFile}`] : []),
      ],
      shell: false,
    };
  }
  return null;
}

const PRIOR_CTX_MAX_TOTAL = 14000;
const PRIOR_CTX_MAX_TASK = 2500;
const PRIOR_CTX_MAX_FINAL = 9000;
const PRIOR_CTX_TAIL_STEPS = 12;
const PRIOR_CTX_MAX_STEP_SUMMARY = 500;

/**
 * 从上一任务的 trajectory JSON 生成前置文本，注入到新 Trae 指令前以延续「会话」语义。
 * 轨迹路径约定与 runJobAsync 写入一致：layer_artifacts/{layer}/.trajectories/trajectory_{jobId}.json
 */
function loadPriorTrajectoryContextPrefix(priorJobId) {
  const jid = String(priorJobId || '').trim();
  if (!jid) return '';
  const j = jobs.get(jid);
  if (!j || j.command_kind === 'clone') return '';
  let trajPath;
  try {
    trajPath = path.join(layerArtifactsDir(j.layer_id), '.trajectories', `trajectory_${jid}.json`);
  } catch {
    return '';
  }
  if (!fs.existsSync(trajPath)) return '';
  let raw;
  try {
    raw = fs.readFileSync(trajPath, 'utf8');
  } catch {
    return '';
  }
  let doc;
  try {
    doc = JSON.parse(raw);
  } catch {
    return '';
  }
  if (!doc || typeof doc !== 'object') return '';

  const trunc = (s, n) => {
    const t = String(s ?? '').trim();
    if (!t) return '';
    return t.length <= n ? t : t.slice(0, n - 1) + '…';
  };

  const task = trunc(doc.task, PRIOR_CTX_MAX_TASK);
  const finalResult = trunc(doc.final_result, PRIOR_CTX_MAX_FINAL);

  const steps = Array.isArray(doc.agent_steps) ? doc.agent_steps : [];
  const tail = steps.slice(-PRIOR_CTX_TAIL_STEPS);
  const stepLines = tail
    .map((s, idx) => {
      const sn = s && s.step_number != null ? s.step_number : idx + 1;
      const sum = trunc(s.delivery_summary || s.reflection || '', PRIOR_CTX_MAX_STEP_SUMMARY);
      return sum ? `- 步骤 ${sn}: ${sum}` : '';
    })
    .filter(Boolean);

  const parts = [
    '<<< PRIOR_AGENT_SESSION_CONTEXT >>>',
    '以下内容为同一工作区上一段 AI 任务的轨迹摘要，请在回答新指令时继承其中的结论与约束（除非新指令明确要求推翻）。',
    task ? `上一任务指令:\n${task}` : '',
    finalResult ? `上一任务最终结果摘要:\n${finalResult}` : '',
    stepLines.length ? `上一任务后续关键步骤:\n${stepLines.join('\n')}` : '',
    '<<< END_PRIOR_CONTEXT >>>',
  ].filter(Boolean);

  let block = parts.join('\n\n');
  if (block.length > PRIOR_CTX_MAX_TOTAL) block = block.slice(0, PRIOR_CTX_MAX_TOTAL - 1) + '…';
  return block ? `${block}\n\n` : '';
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
  delete layerQueues[lid];
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
  let prior_context_job_id = '';
  if (parent_job_id) {
    const p = jobs.get(parent_job_id);
    if (!p) throw new Error(`parent_job_id not found: ${parent_job_id}`);
    stackParent = p.layer_id;
    prior_context_job_id = parent_job_id;
  } else {
    if (!fs.existsSync(layerPath(repo_layer_id))) throw new Error(`repo_layer_id not found: ${repo_layer_id}`);
    stackParent = repo_layer_id;
    const pc = body.prior_context_job_id ? String(body.prior_context_job_id).trim() : '';
    prior_context_job_id = pc;
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
    prior_context_job_id: prior_context_job_id || null,
  };
  jobs.set(id, rec);
  saveState();
  broadcast({ type: 'job_created', job_id: id, layer_id: lid });
  await mirrorLayerGraphToTaskCloudSSE();
  recordJobEvent(id, 'start');
  runJobAsync(rec, work);
  return rec;
}

/**
 * UI：运行中任务所在层上「加入队列」。当前任务正常结束（非用户中断）后，按顺序以该层为父叠建新层并执行。
 * @returns {{ ok: true, layer_id: string, queue_position: number, queue_depth: number }}
 */
export function removeLayerQueue(layerId) {
  const lid = String(layerId || '').trim();
  if (!lid || !layerQueues[lid]) return;
  delete layerQueues[lid];
  saveState();
}

export function enqueueLayerQueueItem(layerId, body) {
  const lid = String(layerId || '').trim();
  if (!lid) throw new Error('layer_id 无效');
  if (!fs.existsSync(layerPath(lid))) throw new Error(`layer not found: ${lid}`);
  const command = String(body?.command || '').trim();
  if (!command) throw new Error('command 不能为空');
  const command_kind = String(body?.command_kind || 'trae').toLowerCase();
  if (!['trae', 'shell'].includes(command_kind)) throw new Error('invalid command_kind');
  if (!anyLayerHasGitRepo()) throw new Error('请先完成「克隆仓库」后再创建任务。');
  if (command_kind === 'trae' && !fs.existsSync(configFilePath())) {
    throw new Error(`Config missing: ${configFilePath()}`);
  }
  const env = body?.env && typeof body.env === 'object' ? body.env : null;
  if (!layerQueues[lid]) layerQueues[lid] = [];
  layerQueues[lid].push({ command, command_kind, env });
  const depth = layerQueues[lid].length;
  saveState();
  broadcast({ type: 'layer_queue_enqueued', layer_id: lid, queue_depth: depth });
  void mirrorLayerGraphToTaskCloudSSE().catch(() => {});
  return { ok: true, layer_id: lid, queue_position: depth - 1, queue_depth: depth };
}

async function drainQueuedJobsForLayer(completedLayerId, completedJobId) {
  const lid = String(completedLayerId || '').trim();
  if (!lid) return;
  const q = layerQueues[lid];
  if (!q || !q.length) return;
  const next = q[0];
  const rest = q.slice(1);
  delete layerQueues[lid];
  saveState();
  const finishedId = completedJobId ? String(completedJobId).trim() : '';
  try {
    const rec = await createJob({
      repo_layer_id: lid,
      command: next.command,
      command_kind: next.command_kind,
      ...(next.env ? { env: next.env } : {}),
      ...(finishedId ? { prior_context_job_id: finishedId } : {}),
    });
    if (rest.length) {
      layerQueues[rec.layer_id] = rest;
      saveState();
      broadcast({ type: 'layer_queue_moved', from_layer_id: lid, to_layer_id: rec.layer_id, queue_depth: rest.length });
    }
  } catch (e) {
    console.error(`[jobsRuntime] drain queue failed for layer ${lid}:`, e);
    broadcast({
      type: 'layer_queue_drain_failed',
      layer_id: lid,
      detail: String(e.message || e),
    });
  }
}

function runJobAsync(rec, workDir) {
  resetExecStream('job', rec.id);
  const env = { ...process.env, PYTHONUNBUFFERED: '1' };
  let trajectoryFile;
  if (rec.command_kind === 'trae') {
    const trajDir = path.join(layerArtifactsDir(rec.layer_id), '.trajectories');
    fs.mkdirSync(trajDir, { recursive: true });
    trajectoryFile = path.join(trajDir, `trajectory_${rec.id}.json`);
    env.TRAE_AGENT_JSON_OUTPUT_DIR = jobLogsTaeJsonDir(rec.id);
  }
  if (rec.command_env && typeof rec.command_env === 'object') {
    for (const [k, v] of Object.entries(rec.command_env)) {
      if (v != null) env[String(k)] = String(v);
    }
  }
  let commandForTrae = rec.command;
  if (rec.command_kind === 'trae') {
    const prefix = loadPriorTrajectoryContextPrefix(rec.prior_context_job_id || rec.parent_job_id);
    if (prefix) commandForTrae = prefix + rec.command;
  }

  let proc;
  if (rec.command_kind === 'shell') {
    proc = spawn('bash', ['-lc', rec.command], { cwd: workDir, env });
  } else {
    const trae = buildTraeCmd(workDir, commandForTrae, { trajectoryFile });
    if (trae) {
      proc = spawn(trae.cmd, trae.args, { cwd: workDir, env, shell: trae.shell || false });
    } else {
      proc = spawn(
        'bash',
        [
          '-lc',
          `echo "[onlineServiceJS] 未找到 trae-cli（请确认镜像已安装 /app/.venv，或设置 TRAE_CLI / TRAE_VENV）。占位未执行指令: ${commandForTrae.replace(/'/g, "'\\''")}" >&2; exit 1`,
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
      recordJobEvent(rec.id, 'chunk', t);
    });
    proc.stderr?.on('data', (c) => {
      const t = c.toString();
      rec.output = (rec.output || '') + t;
      appendExecStream('job', rec.id, t);
      recordJobEvent(rec.id, 'chunk', t);
    });
  } catch {
    /* ignore */
  }
  rec.status = 'running';
  running.set(rec.id, proc);
  saveState();
  broadcast({ type: 'job_started', job_id: rec.id });
  recordJobEvent(rec.id, 'running');
  void mirrorLayerGraphToTaskCloudSSE().catch(() => {});
  proc.on('close', (code) => {
    running.delete(rec.id);
    rec.exit_code = code;
    const wasInterrupted = rec.status === 'interrupted';
    if (!wasInterrupted) {
      rec.status = code === 0 ? 'completed' : 'failed';
    }
    completeExecStream('job', rec.id);
    saveState();
    broadcast({ type: 'job_finished', job_id: rec.id, status: rec.status, exit_code: code });
    const finalPhase = wasInterrupted ? 'interrupted' : (code === 0 ? 'completed' : 'failed');
    recordJobEvent(rec.id, finalPhase);
    /** 任务结束或中断后同步层级快照至任务云 SSE，避免详情页 zTree 仍显示「运行中」。 */
    void mirrorLayerGraphToTaskCloudSSE().catch(() => {});
    if (!wasInterrupted) {
      void drainQueuedJobsForLayer(rec.layer_id, rec.id);
    }
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
    void mirrorLayerGraphToTaskCloudSSE().catch(() => {});
    void drainQueuedJobsForLayer(rec.layer_id, rec.id);
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
  void mirrorLayerGraphToTaskCloudSSE().catch(() => {});
  return rec;
}

export function deleteJob(jobId) {
  const rec = jobs.get(jobId);
  if (!rec) throw new Error('job not found');
  interruptJob(jobId);
  try {
    fs.rmSync(jobLogsTaeJsonPath(rec.id), { recursive: true, force: true });
  } catch {
    /* ignore */
  }
  delete layerQueues[rec.layer_id];
  deleteLayerTree(rec.layer_id);
  jobs.delete(jobId);
  saveState();
  void mirrorLayerGraphToTaskCloudSSE().catch(() => {});
  return { ok: true };
}

/**
 * 只把「有合法 meta、克隆进行中、或已有仓库内容」的目录计为可写层。
 * 避免残留空目录名符合 layer id 时混入 GET /api/layers，在图上多出一个与 clone 同级的伪节点。
 */
export function layerIdQualifiesForSnapshot(layerId) {
  const lid = String(layerId || '').trim();
  if (!lid) return false;
  const p = layerPath(lid);
  let stDir;
  try {
    stDir = fs.existsSync(p) ? fs.statSync(p) : null;
  } catch {
    return false;
  }
  if (!stDir || !stDir.isDirectory()) return false;
  const m = readLayerMeta(lid);
  if (m && m.kind) return true;
  const op = getCloneOpStatus(lid);
  if (op && (op.status === 'queued' || op.status === 'running')) return true;
  try {
    if (fs.existsSync(path.join(p, 'base'))) return true;
  } catch {
    /* ignore */
  }
  if (layerRootOrChildHasGit(p)) return true;
  return false;
}

/**
 * 启动时移除非「可写层」的残留 layer 子目录，避免与真实克隆层在 UI 上重复出现。
 */
export function sweepDanglingLayerDirs() {
  const all = listLayerRows();
  for (const row of all) {
    const lid = row.layer_id;
    if (layerIdQualifiesForSnapshot(lid)) continue;
    const op = getCloneOpStatus(lid);
    if (op && (op.status === 'queued' || op.status === 'running')) continue;
    try {
      deleteLayerTree(lid);
    } catch {
      /* ignore */
    }
  }
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
    prior_context_job_id: null,
  };
  jobs.set(id, rec);
  saveState();
}

export function buildLayersSnapshot(bootstrapLayerId) {
  const rows = listLayerRows().filter((r) => layerIdQualifiesForSnapshot(r.layer_id));
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
    const qArr = Array.isArray(layerQueues[lid]) ? layerQueues[lid] : [];
    const queue_items = qArr.map((entry, position) => {
      const cmd = String(entry.command || '');
      const command_preview = cmd.length > 72 ? cmd.slice(0, 72) + '…' : cmd;
      return {
        position,
        command_kind: entry.command_kind || 'trae',
        command_preview,
      };
    });
    const item = {
      layer_id: lid,
      created_at: row.created_at,
      command: displayCmd,
      parent_layer_id: resolvedParentLayerId(lid, known, jobsList),
      job_id: jobByLayer[lid]?.id || null,
      job_status: jobByLayer[lid]?.status || null,
      queue_depth: qArr.length,
      queue_items,
      mind_state: jobByLayer[lid]?.status === 'running' || jobByLayer[lid]?.status === 'pending' ? 'running' : 'idle_done',
      git_worktree_dirty: gitWorktreeDirty(lid),
      git_remote: layerGitRemoteSnapshot(lid),
      meta_kind: meta?.kind || null,
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

/** 将当前层级图镜像到任务云 SSE（container_layer_graph），供 Vue 任务详情与容器内 GET /api/events/stream 解耦。 */
export async function mirrorLayerGraphToTaskCloudSSE() {
  await publishLayerGraphSnapshotToSaas(buildLayersSnapshot(bootstrapCloneLayerId));
}

loadState();
