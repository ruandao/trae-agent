/**
 * 从 ONLINE_PROJECT_STATE_ROOT 读取 Trae agent 步骤，供 GET /api/jobs/:id/steps。
 * 仅支持 runtime/layer_artifacts 与 runtime/job_logs/trae_agent_json，不读层工作区目录。
 */
import fs from 'fs';
import path from 'path';
import { stateRoot, layerArtifactsRootPath, jobLogsTaeJsonPath } from './paths.mjs';

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

function lakeviewSummaryFromFile(stepDir) {
  const lv = safeReadJson(path.join(stepDir, 'lakeview_step.json'));
  if (!lv || typeof lv !== 'object') return null;
  const parts = [lv.desc_task, lv.desc_details, lv.tags_emoji ? String(lv.tags_emoji) : '']
    .filter((x) => x != null && String(x).trim())
    .map((x) => String(x).trim());
  return parts.length ? parts.join('\n') : null;
}

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

function normalizeAgentStep(step) {
  if (!step || typeof step !== 'object') return step;
  const s = { ...step };
  if ((!s.tool_calls || !s.tool_calls.length) && s.llm_response && Array.isArray(s.llm_response.tool_calls)) {
    s.tool_calls = s.llm_response.tool_calls;
  }
  s.tool_results = normalizeToolResults(s.tool_results);
  return s;
}

function loadStepsFromTrajectoriesInDir(trajDir, relBase) {
  const trajFile = newestMtimeFile(
    trajDir,
    (n) => n.startsWith('trajectory_') && n.endsWith('.json'),
  );
  if (!trajFile) return null;
  const raw = safeReadJson(trajFile);
  if (!raw || typeof raw !== 'object') return null;
  const steps = Array.isArray(raw.agent_steps) ? raw.agent_steps.map(normalizeAgentStep) : [];
  const rel = path.relative(relBase, trajFile).split(path.sep).join('/');
  return {
    steps,
    trajectory_file: rel,
    task: raw.task != null ? String(raw.task) : null,
    note: steps.length ? null : '轨迹文件中 agent_steps 为空',
  };
}

const STEP_DIR_RE = /^step_(\d+)$/;

function loadStepsFromTaeJsonOutputDir(outputRoot) {
  if (!outputRoot || !fs.existsSync(outputRoot)) return null;
  const stepDirs = [];
  for (const name of fs.readdirSync(outputRoot)) {
    const m = name.match(STEP_DIR_RE);
    if (!m) continue;
    stepDirs.push({ num: parseInt(m[1], 10), dir: path.join(outputRoot, name) });
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
  const rel = path.relative(stateRoot(), outputRoot).split(path.sep).join('/');
  return {
    steps,
    trajectory_file: rel,
    task: null,
    note: null,
  };
}

/**
 * @param {string} layerId
 * @param {string} [jobId]
 * @returns {{ steps: object[], note: string | null, trajectory_file: string | null, task: string | null }}
 */
export function getJobStepsForLayer(layerId, jobId) {
  const lid = String(layerId || '').trim();
  if (!lid) {
    return {
      steps: [],
      note: '缺少 layer_id',
      trajectory_file: null,
      task: null,
    };
  }
  const sr = stateRoot();
  const jid = jobId != null && String(jobId).trim() ? String(jobId).trim() : '';

  if (jid) {
    const exactTraj = path.join(layerArtifactsRootPath(lid), '.trajectories', `trajectory_${jid}.json`);
    if (fs.existsSync(exactTraj)) {
      const raw = safeReadJson(exactTraj);
      if (raw && typeof raw === 'object') {
        const steps = Array.isArray(raw.agent_steps) ? raw.agent_steps.map(normalizeAgentStep) : [];
        if (steps.length) {
          return {
            steps,
            note: null,
            trajectory_file: path.relative(sr, exactTraj).split(path.sep).join('/'),
            task: raw.task != null ? String(raw.task) : null,
          };
        }
      }
    }
    const fromTae = loadStepsFromTaeJsonOutputDir(jobLogsTaeJsonPath(jid));
    if (fromTae && fromTae.steps.length) {
      return {
        steps: fromTae.steps,
        note: fromTae.note,
        trajectory_file: fromTae.trajectory_file,
        task: fromTae.task,
      };
    }
  }

  const stateTrajDir = path.join(layerArtifactsRootPath(lid), '.trajectories');
  if (fs.existsSync(stateTrajDir)) {
    const fromState = loadStepsFromTrajectoriesInDir(stateTrajDir, sr);
    if (fromState && fromState.steps.length) {
      return {
        steps: fromState.steps,
        note: fromState.note,
        trajectory_file: fromState.trajectory_file,
        task: fromState.task,
      };
    }
  }

  return {
    steps: [],
    note: '未找到步骤：请确认 onlineProject_state 下存在 runtime/layer_artifacts 或 runtime/job_logs/trae_agent_json 数据',
    trajectory_file: null,
    task: null,
  };
}
