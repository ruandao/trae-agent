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

export const reqLogsDir = () => {
  const d = path.join(stateRoot(), 'reqLogs');
  fs.mkdirSync(d, { recursive: true });
  return d;
};
