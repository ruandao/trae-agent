// @ts-check
/**
 * 验证 DELETE /api/layers/:id 后向任务云上报 layer-graph-push（任务详情 zTree 经 SSE 同步删层）。
 *
 * npx playwright test --project=api e2e/layer-graph-push-on-layer-delete.api.spec.mjs
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import http from 'http';
import { spawn, spawnSync } from 'child_process';
import { fileURLToPath } from 'url';
import { test, expect } from '@playwright/test';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SERVICE_ROOT = path.resolve(__dirname, '..');

const BASE_LAYER_ID = '20260204_120000_a1b2c3';
const CHILD_LAYER_ID = '20260204_120100_c4d5e6';
const ACCESS = 'e2e-layer-delete-token';

function mkdirDeepSync(d) {
  fs.mkdirSync(d, { recursive: true });
}

function seedLayer(layersDir, layerId, parentLayerId = null) {
  const dir = path.join(layersDir, layerId);
  mkdirDeepSync(dir);
  fs.writeFileSync(
    path.join(dir, 'layer_meta.json'),
    JSON.stringify(
      { version: 1, kind: 'job', parent_layer_id: parentLayerId },
      null,
      2,
    ),
    'utf8',
  );
  const gitInit = spawnSync('git', ['init'], { cwd: dir, encoding: 'utf8' });
  expect(gitInit.status, gitInit.stderr || '').toBe(0);
}

/** @returns {Promise<{ server: import('http').Server, pushes: object[], port: number }>} */
function startMockTaskCloud() {
  /** @type {object[]} */
  const pushes = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      const url = req.url || '';
      try {
        if (req.method === 'POST' && url.includes('layer-graph-push')) {
          pushes.push(raw ? JSON.parse(raw) : {});
        }
      } catch {
        pushes.push({ _parse_error: true, raw: raw.slice(0, 400) });
      }
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      if (url.includes('exchange-refresh')) {
        res.end(JSON.stringify({ refresh_token: 'mock-refresh' }));
        return;
      }
      if (url.includes('refresh-access')) {
        res.end(JSON.stringify({ access_token: ACCESS }));
        return;
      }
      if (url.includes('task-detail')) {
        res.end(JSON.stringify({ repo_clone_credentials: {} }));
        return;
      }
      if (url.includes('feature-params-env')) {
        res.end(
          JSON.stringify({
            env: {
              TASK_LLM_PROVIDERS_JSON: '[]',
              TASK_AGENT_MAX_STEPS: '200',
            },
          }),
        );
        return;
      }
      res.end('{}');
    });
  });
  return new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      resolve({ server, pushes, port });
    });
  });
}

function pickFreeListenPort() {
  return new Promise((resolve, reject) => {
    const s = http.createServer();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const addr = s.address();
      const p = typeof addr === 'object' && addr ? addr.port : 0;
      s.close(() => resolve(p));
    });
  });
}

function waitForHttpOk(url, headers, timeoutMs) {
  const start = Date.now();
  return /** @type {Promise<void>} */ (
    new Promise((resolve, reject) => {
      const tick = () => {
        if (Date.now() - start > timeoutMs) {
          reject(new Error(`timeout waiting for ${url}`));
          return;
        }
        fetch(url, { headers })
          .then((r) => {
            if (r.ok) resolve();
            else setTimeout(tick, 150);
          })
          .catch(() => setTimeout(tick, 150));
      };
      tick();
    })
  );
}

test.describe.configure({ mode: 'serial', timeout: 120_000 });

test('DELETE /api/layers/:id triggers layer-graph-push without deleted layer', async ({ request }) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'osjs-lg-del-'));
  const layersDir = path.join(tmp, 'layers');
  mkdirDeepSync(layersDir);
  seedLayer(layersDir, BASE_LAYER_ID, null);
  seedLayer(layersDir, CHILD_LAYER_ID, BASE_LAYER_ID);

  const mock = await startMockTaskCloud();
  const listenPort = await pickFreeListenPort();

  const childEnv = {
    ...process.env,
    NODEJS_WATCH: '0',
    TRAE_ONLINE_JS_DOCKER: '0',
    TRAE_USE_OVERLAY_STACK: '0',
    PORT: String(listenPort),
    ONLINE_PROJECT_STATE_ROOT: tmp,
    ONLINE_PROJECT_LAYERS: layersDir,
    REPO_ROOT: tmp,
    ACCESS_TOKEN: ACCESS,
    BusinessApiEndPoint: `http://127.0.0.1:${listenPort}/api`,
    TaskApiEndPoint: `http://127.0.0.1:${mock.port}`,
    tenantId: '1',
    workspaceId: '1',
    taskId: '1',
    NO_PROXY: '*',
    TASK_API_BOOTSTRAP_STRICT_STARTUP: '0',
  };

  const proc = spawn(process.execPath, ['src/server.mjs'], {
    cwd: SERVICE_ROOT,
    env: childEnv,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  try {
    const gateUrl = `http://127.0.0.1:${listenPort}/api/requirements/task-gate`;
    await waitForHttpOk(gateUrl, { 'X-Access-Token': ACCESS }, 45_000);

    const tokHeader = { 'X-Access-Token': ACCESS };
    const delRes = await request.delete(
      `http://127.0.0.1:${listenPort}/api/layers/${encodeURIComponent(CHILD_LAYER_ID)}`,
      { headers: tokHeader },
    );
    expect(delRes.ok(), await delRes.text()).toBeTruthy();

    await expect
      .poll(
        () =>
          mock.pushes.some((p) => {
            const layers = Array.isArray(p.layers) ? p.layers : [];
            const ids = layers.map((x) => (x && x.layer_id ? String(x.layer_id) : ''));
            return ids.includes(BASE_LAYER_ID) && !ids.includes(CHILD_LAYER_ID);
          }),
        { timeout: 15_000 },
      )
      .toBe(true);

    expect(fs.existsSync(path.join(layersDir, CHILD_LAYER_ID))).toBe(false);
  } finally {
    proc.kill('SIGTERM');
    await new Promise((r) => setTimeout(r, 400));
    try {
      proc.kill('SIGKILL');
    } catch {
      /* ignore */
    }
    await new Promise((resolve) => mock.server.close(resolve));
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
});
