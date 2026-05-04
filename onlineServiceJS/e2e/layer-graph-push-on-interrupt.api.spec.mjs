// @ts-check
/**
 * 启动 mock 任务云（接收 layer-graph-push）与独立 onlineServiceJS 子进程，验证 POST interrupt 后
 * mock 收到含 interrupted 的 jobs 快照（修复：此前仅在 createJob/入队等处上报，详情页 zTree 仍显示运行中）。
 *
 * npx playwright test --project=api e2e/layer-graph-push-on-interrupt.api.spec.mjs
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
const ACCESS = 'e2e-layer-graph-token';

function mkdirDeepSync(d) {
  fs.mkdirSync(d, { recursive: true });
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
      if (url.includes('feature-params-yaml')) {
        res.end(JSON.stringify({ yaml: 'noop: true\n' }));
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

test('POST /api/jobs/:id/interrupt triggers layer-graph-push with interrupted job', async ({ request }) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'osjs-lg-int-'));
  const layersDir = path.join(tmp, 'layers');
  mkdirDeepSync(layersDir);
  const baseDir = path.join(layersDir, BASE_LAYER_ID);
  mkdirDeepSync(baseDir);
  fs.writeFileSync(
    path.join(baseDir, 'layer_meta.json'),
    JSON.stringify({ version: 1, kind: 'job', parent_layer_id: null }, null, 2),
    'utf8',
  );
  const gitInit = spawnSync('git', ['init'], { cwd: baseDir, encoding: 'utf8' });
  expect(gitInit.status, gitInit.stderr || '').toBe(0);

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
  const tail = (/** @type {Buffer} */ b) => String(b || '').slice(-800);
  proc.stderr?.on('data', (d) => {
    if (String(process.env.DEBUG_OSJS_E2E || '')) process.stderr.write(tail(d));
  });

  try {
    const gateUrl = `http://127.0.0.1:${listenPort}/api/requirements/task-gate`;
    await waitForHttpOk(gateUrl, { 'X-Access-Token': ACCESS }, 45_000);

    const tokHeader = { 'X-Access-Token': ACCESS, 'Content-Type': 'application/json' };
    const createRes = await request.post(`http://127.0.0.1:${listenPort}/api/jobs`, {
      headers: tokHeader,
      data: JSON.stringify({
        repo_layer_id: BASE_LAYER_ID,
        command: process.platform === 'win32' ? 'timeout /t 90' : 'sleep 90',
        command_kind: 'shell',
      }),
    });
    expect(createRes.ok(), await createRes.text()).toBeTruthy();
    const created = await createRes.json();
    const jobId = String(created.id || '');
    expect(jobId.length).toBeGreaterThan(10);

    /** createJob 只在 pending 时上报快照；进入 running 后须待进程启动再中断，否则 interrupt 不会写入 interrupted */
    await expect
      .poll(
        async () => {
          const r = await request.get(`http://127.0.0.1:${listenPort}/api/jobs/${jobId}`, {
            headers: tokHeader,
          });
          if (!r.ok()) return '';
          const j = await r.json();
          return String(j.status || '');
        },
        { timeout: 15_000 },
      )
      .toBe('running');

    const intRes = await request.post(`http://127.0.0.1:${listenPort}/api/jobs/${jobId}/interrupt`, {
      headers: tokHeader,
    });
    expect(intRes.ok(), await intRes.text()).toBeTruthy();

    await expect
      .poll(
        () =>
          mock.pushes.some((p) =>
            Array.isArray(p.jobs)
              ? p.jobs.some((j) => j && String(j.id) === jobId && j.status === 'interrupted')
              : false,
          ),
        { timeout: 15_000 },
      )
      .toBe(true);
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
