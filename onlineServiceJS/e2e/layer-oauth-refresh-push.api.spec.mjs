// @ts-check
/**
 * Mock task2app 换票 + 层内 git 仓库，验证 POST /api/layers/:id/git/oauth-refresh-push。
 *
 * npx playwright test --project=api e2e/layer-oauth-refresh-push.api.spec.mjs
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

const LAYER_ID = '20260204_120000_oauth1';
const ACCESS = 'e2e-oauth-refresh-token';

function mkdirDeepSync(d) {
  fs.mkdirSync(d, { recursive: true });
}

function startMockTaskCloud() {
  /** @type {object[]} */
  const tokenRequests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      const url = req.url || '';
      let body = {};
      try {
        body = raw ? JSON.parse(raw) : {};
      } catch {
        body = {};
      }
      if (req.method === 'POST' && url.includes('layer-github-oauth-access-tokens')) {
        tokenRequests.push(body);
        res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
        res.end(
          JSON.stringify({
            ok: true,
            github_auth_by_repo: { 'acme/oauth-e2e': 'gho_e2e_mock_token' },
            pr_base_branch: 'main',
            pr_title: 'E2E PR',
            pr_body: 'e2e',
          }),
        );
        return;
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
        res.end(JSON.stringify({ task: { target_branch: 'feature/e2e-oauth' } }));
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
      resolve({ server, tokenRequests, port });
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
  return new Promise((resolve, reject) => {
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
  });
}

function initBareRepo(dir, { origin, branch = 'main', file = 'README.md', content = 'init\n' } = {}) {
  mkdirDeepSync(dir);
  spawnSync('git', ['init', '-b', branch], { cwd: dir, encoding: 'utf8' });
  fs.writeFileSync(path.join(dir, file), content);
  spawnSync('git', ['add', file], { cwd: dir, encoding: 'utf8' });
  spawnSync('git', ['commit', '-m', 'init'], { cwd: dir, encoding: 'utf8' });
  if (origin) {
    spawnSync('git', ['remote', 'add', 'origin', origin], { cwd: dir, encoding: 'utf8' });
  }
}

test.describe.configure({ mode: 'serial', timeout: 120_000 });

test('POST oauth-refresh-push 拉取 token 并尝试 push', async ({ request }) => {
  const mock = await startMockTaskCloud();
  const listenPort = await pickFreeListenPort();
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'oauth-refresh-e2e-'));
  const layerDir = path.join(stateRoot, 'layers', LAYER_ID);
  const repoDir = path.join(layerDir, 'acme-oauth-e2e');
  mkdirDeepSync(layerDir);
  fs.writeFileSync(
    path.join(layerDir, 'layer_meta.json'),
    JSON.stringify({ layer_id: LAYER_ID, kind: 'workspace', created_at: new Date().toISOString() }),
  );
  initBareRepo(repoDir, { origin: 'https://github.com/acme/oauth-e2e.git' });

  const layersDir = path.join(stateRoot, 'layers');
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: SERVICE_ROOT,
    env: {
      ...process.env,
      NODEJS_WATCH: '0',
      TRAE_ONLINE_JS_DOCKER: '0',
      TRAE_USE_OVERLAY_STACK: '0',
      PORT: String(listenPort),
      ACCESS_TOKEN: ACCESS,
      TRAE_SKIP_CONTAINER_TOKEN_EXCHANGE: '1',
      TASK_API_BOOTSTRAP_STRICT_STARTUP: '0',
      ONLINE_PROJECT_STATE_ROOT: stateRoot,
      ONLINE_PROJECT_LAYERS: layersDir,
      REPO_ROOT: stateRoot,
      BusinessApiEndPoint: `http://127.0.0.1:${listenPort}/api`,
      TaskApiEndPoint: `http://127.0.0.1:${mock.port}/api/tenant/t/ws/task1/cloud`,
      tenantId: 't',
      workspaceId: 'ws',
      taskId: 'task1',
      NO_PROXY: '*',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let childLog = '';
  child.stderr?.on('data', (d) => {
    childLog += String(d);
  });
  child.stdout?.on('data', (d) => {
    childLog += String(d);
  });

  const base = `http://127.0.0.1:${listenPort}`;
  const tokHeader = { 'X-Access-Token': ACCESS, 'Content-Type': 'application/json' };
  try {
    await waitForHttpOk(`${base}/api/requirements/task-gate`, tokHeader, 45_000);

    const resp = await request.post(`${base}/api/layers/${LAYER_ID}/git/oauth-refresh-push`, {
      headers: tokHeader,
      data: {},
    });
    const body = await resp.json();
    expect(mock.tokenRequests.length).toBeGreaterThanOrEqual(1);
    expect(mock.tokenRequests[0].repo_slugs).toContain('acme/oauth-e2e');
    expect(mock.tokenRequests[0].target_branch).toBe('feature/e2e-oauth');

    if (resp.ok()) {
      expect(body.ok).toBe(true);
    } else {
      expect(body.detail).toBeTruthy();
    }
  } finally {
    child.kill('SIGTERM');
    await new Promise((r) => child.on('close', r));
    if (child.exitCode && child.exitCode !== 0 && childLog) {
      console.error('[oauth-refresh-push e2e] child log:\n', childLog.slice(-2000));
    }
    mock.server.close();
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});
