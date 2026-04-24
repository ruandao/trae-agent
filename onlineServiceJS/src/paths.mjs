import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** onlineServiceJS 包根（含 package.json） */
export const serviceRoot = () => path.resolve(__dirname, '..');

export const repoRoot = () =>
  path.resolve(process.env.REPO_ROOT || path.resolve(serviceRoot(), '..'));

export const stateRoot = () => {
  const raw = process.env.ONLINE_PROJECT_STATE_ROOT;
  const root = raw ? path.resolve(raw) : path.join(repoRoot(), 'onlineProject_state');
  fs.mkdirSync(root, { recursive: true });
  return root;
};

export const runtimeDir = () => {
  const d = path.join(stateRoot(), 'runtime');
  fs.mkdirSync(d, { recursive: true });
  return d;
};

export const logsDir = () => {
  const d = path.join(stateRoot(), 'logs');
  fs.mkdirSync(d, { recursive: true });
  return d;
};

export const layersRoot = () => {
  const raw = process.env.ONLINE_PROJECT_LAYERS;
  const root = raw ? path.resolve(raw) : path.join(stateRoot(), 'layers');
  fs.mkdirSync(root, { recursive: true });
  return root;
};

export const configFilePath = () => path.join(runtimeDir(), 'service_config.yaml');

export const jobsStatePath = () => path.join(runtimeDir(), 'jobs_state.json');

/** 可写层在 state 根下的智能体侧车根路径（与 onlineProject 解耦，不含仅 mkdir 以外的副作用） */
export const layerArtifactsRootPath = (layerId) => {
  const lid = String(layerId || '').trim();
  if (!lid) throw new Error('layer_id required');
  return path.join(runtimeDir(), 'layer_artifacts', lid);
};

export const layerArtifactsDir = (layerId) => {
  const d = layerArtifactsRootPath(layerId);
  fs.mkdirSync(d, { recursive: true });
  return d;
};

export const jobLogsTaeJsonPath = (jobId) => {
  const jid = String(jobId || '').trim();
  if (!jid) throw new Error('job_id required');
  return path.join(runtimeDir(), 'job_logs', 'trae_agent_json', jid);
};

export const jobLogsTaeJsonDir = (jobId) => {
  const d = jobLogsTaeJsonPath(jobId);
  fs.mkdirSync(d, { recursive: true });
  return d;
};

export const reqLogsDir = () => {
  const d = path.join(stateRoot(), 'reqLogs');
  fs.mkdirSync(d, { recursive: true });
  return d;
};
