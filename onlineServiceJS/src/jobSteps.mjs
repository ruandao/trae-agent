/**
 * 从任务可写层工作区读取 Trae agent 步骤（轨迹 JSON 或 file_log），供 GET /api/jobs/:id/steps。
 */
import fs from 'fs';
import path from 'path';
import { layerPath, layerPrimaryGitWorkdir } from './layerFs.mjs';

function safeReadJson(p) {
  try {
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch {
    return null;
  }
}

function newestMtimeFile(dir, predicate) {
  if (!fs.existsSync(dir)) return null;
  let best = null;
  let bestT = -1;
  for (const name of fs.readdirSync(dir)) {
    if (!predicate(name)) continue;
    const fp = path.join(dir, name);
    let st;
    try {
      st = fs.statSync(fp);
    } catch {
      continue;
    }
    if (!st.isFile()) continue;
    if (st.mtimeMs >= bestT) {
      bestT = st.mtimeMs;
      best = fp;
    }
  }
  return best;
}

function newestMtimeDir(dir, predicate) {
  if (!fs.existsSync(dir)) return null;
  let best = null;
  let bestT = -1;
  for (const name of fs.readdirSync(dir)) {
    if (!predicate(name)) continue;
    const fp = path.join(dir, name);
    let st;
    try {
      st = fs.statSync(fp);
    } catch {
      continue;
    }
    if (!st.isDirectory()) continue;
    if (st.mtimeMs >= bestT) {
      bestT = st.mtimeMs;
      best = fp;
    }
  }
  return best;
}

function lakeviewSummaryFromFile(stepDir) {
  const lv = safeReadJson(path.join(stepDir, 'lakeview_step.json'));
  if (!lv || typeof lv !== 'object') return null;
  const parts = [lv.desc_task, lv.desc_details, lv.tags_emoji ? String(lv.tags_emoji) : '']
    .filter((x) => x != null && String(x).trim())
    .map((x) => String(x).trim());
  return parts.length ? parts.join('\n') : null;
}

/** 统一 tool_results 条目字段，供前端按 call_id 关联 */
function normalizeToolResults(rows) {
  if (!Array.isArray(rows)) return [];
  return rows.map((r) => {
    if (!r || typeof r !== 'object') return r;
    const out = { ...r };
    if (out.error == null && out.success === false && out.result != null) {
      out.error = String(out.result);
    }
    return out;
  });
}

/**
 * 补全 agent_step_full 中与轨迹 agent_steps 略有差异的字段。
 */
function normalizeAgentStep(step) {
  if (!step || typeof step !== 'object') return step;
  const s = { ...step };
  if ((!s.tool_calls || !s.tool_calls.length) && s.llm_response && Array.isArray(s.llm_response.tool_calls)) {
    s.tool_calls = s.llm_response.tool_calls;
  }
  s.tool_results = normalizeToolResults(s.tool_results);
  return s;
}

function loadStepsFromTrajectories(workDir) {
  const trajDir = path.join(workDir, '.trajectories');
  const trajFile = newestMtimeFile(
    trajDir,
    (n) => n.startsWith('trajectory_') && n.endsWith('.json'),
  );
  if (!trajFile) return null;
  const raw = safeReadJson(trajFile);
  if (!raw || typeof raw !== 'object') return null;
  const steps = Array.isArray(raw.agent_steps) ? raw.agent_steps.map(normalizeAgentStep) : [];
  const rel = path.relative(workDir, trajFile).split(path.sep).join('/');
  return {
    steps,
    trajectory_file: rel,
    task: raw.task != null ? String(raw.task) : null,
    note: steps.length ? null : '轨迹文件中 agent_steps 为空',
  };
}

const STEP_DIR_RE = /^step_(\d+)$/;

function loadStepsFromFileLog(workDir) {
  const logRoot = path.join(workDir, '.trae_agent_file_log');
  const runDir = newestMtimeDir(logRoot, (n) => n.startsWith('run_'));
  if (!runDir) return null;

  const stepDirs = [];
  for (const name of fs.readdirSync(runDir)) {
    const m = name.match(STEP_DIR_RE);
    if (!m) continue;
    stepDirs.push({ num: parseInt(m[1], 10), dir: path.join(runDir, name) });
  }
  stepDirs.sort((a, b) => a.num - b.num);

  const steps = [];
  for (const { num, dir } of stepDirs) {
    let fullPath = path.join(dir, 'agent_step_full.json');
    if (!fs.existsSync(fullPath)) fullPath = path.join(dir, 'agent_step.json');
    if (!fs.existsSync(fullPath)) continue;
    const doc = safeReadJson(fullPath);
    if (!doc || typeof doc !== 'object') continue;
    const merged = normalizeAgentStep(doc);
    const lv = lakeviewSummaryFromFile(dir);
    if (lv) merged.lakeview_summary = lv;
    if (merged.step_number == null) merged.step_number = num;
    steps.push(merged);
  }

  if (!steps.length) return null;
  const rel = path.relative(workDir, runDir).split(path.sep).join('/');
  return {
    steps,
    trajectory_file: rel,
    task: null,
    note: null,
  };
}

/**
 * @param {string} layerId
 * @returns {{ steps: object[], note: string | null, trajectory_file: string | null, task: string | null }}
 */
export function getJobStepsForLayer(layerId) {
  const lid = String(layerId || '').trim();
  if (!lid) {
    return {
      steps: [],
      note: '缺少 layer_id',
      trajectory_file: null,
      task: null,
    };
  }
  const workDir = layerPrimaryGitWorkdir(lid) || layerPath(lid);
  if (!workDir || !fs.existsSync(workDir)) {
    return {
      steps: [],
      note: '任务层工作目录不存在',
      trajectory_file: null,
      task: null,
    };
  }

  const fromTraj = loadStepsFromTrajectories(workDir);
  if (fromTraj && fromTraj.steps.length) {
    return {
      steps: fromTraj.steps,
      note: fromTraj.note,
      trajectory_file: fromTraj.trajectory_file,
      task: fromTraj.task,
    };
  }

  const fromLog = loadStepsFromFileLog(workDir);
  if (fromLog && fromLog.steps.length) {
    return {
      steps: fromLog.steps,
      note: fromLog.note,
      trajectory_file: fromLog.trajectory_file,
      task: fromLog.task,
    };
  }

  return {
    steps: [],
    note:
      fromTraj?.note ||
      '未找到步骤：请确认工作区内存在 .trajectories/trajectory_*.json（含 agent_steps）或 .trae_agent_file_log/run_*/step_*',
    trajectory_file: fromTraj?.trajectory_file || null,
    task: fromTraj?.task || null,
  };
}
