import { test, mock, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import http from 'http';
import { spawnSync } from 'child_process';

const ENV_KEYS = [
  'TaskApiEndPoint',
  'TASK_API_ENDPOINT',
  'tenantId',
  'workspaceId',
  'taskId',
  'ACCESS_TOKEN',
  'ONLINE_PROJECT_STATE_ROOT',
  'TRAE_LAYER_GITHUB_OAUTH_FETCH_TIMEOUT_SEC',
];

function saveEnv() {
  const snap = {};
  for (const k of ENV_KEYS) snap[k] = process.env[k];
  return snap;
}

function restoreEnv(snap) {
  for (const k of ENV_KEYS) {
    if (snap[k] === undefined) delete process.env[k];
    else process.env[k] = snap[k];
  }
}

function prepareLayerWithGithubRepo(prefix) {
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), `${prefix}-`));
  process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
  const layerId = `${prefix}-layer`;
  const layerDir = path.join(stateRoot, 'layers', layerId);
  const repoDir = path.join(layerDir, 'demo-repo');
  fs.mkdirSync(repoDir, { recursive: true });
  fs.writeFileSync(
    path.join(layerDir, 'layer_meta.json'),
    JSON.stringify({ layer_id: layerId, kind: 'workspace' }),
  );
  assert.equal(spawnSync('git', ['init'], { cwd: repoDir, encoding: 'utf8' }).status, 0);
  assert.equal(
    spawnSync('git', ['remote', 'add', 'origin', 'https://github.com/acme/demo.git'], {
      cwd: repoDir,
      encoding: 'utf8',
    }).status,
    0,
  );
  return {
    stateRoot,
    layerId,
    repoDir,
    gitPushLog: path.join(stateRoot, 'logs', 'gitPush.log'),
  };
}

describe('layerGitOauthRefreshPush', () => {
  let envSnap;

  beforeEach(() => {
    envSnap = saveEnv();
  });

  afterEach(() => {
    restoreEnv(envSnap);
    mock.restoreAll();
  });

  test('collectGithubRepoSlugsInLayer 解析层内 github remote', async () => {
    const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'oauth-slug-'));
    process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
    const layerId = '20260204_120000_test01';
    const layerDir = path.join(stateRoot, 'layers', layerId);
    const repoDir = path.join(layerDir, 'demo-repo');
    fs.mkdirSync(repoDir, { recursive: true });
    fs.writeFileSync(
      path.join(layerDir, 'layer_meta.json'),
      JSON.stringify({ layer_id: layerId, kind: 'workspace' }),
    );
    assert.equal(spawnSync('git', ['init'], { cwd: repoDir, encoding: 'utf8' }).status, 0);
    spawnSync('git', ['remote', 'add', 'origin', 'https://github.com/acme/demo.git'], {
      cwd: repoDir,
      encoding: 'utf8',
    });

    const mod = await import(`./layerGitOauthRefreshPush.mjs?c=${Date.now()}`);
    const slugs = mod.collectGithubRepoSlugsInLayer(layerId);
    assert.deepEqual(slugs, ['acme/demo']);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  });

  test('runLayerOauthRefreshPush 缺少 ACCESS_TOKEN 时 503', async () => {
    process.env.TaskApiEndPoint =
      'http://127.0.0.1:59999/api/tenant/t1/workspace/w1/task/task1/cloud';
    delete process.env.ACCESS_TOKEN;
    const mod = await import(`./layerGitOauthRefreshPush.mjs?noaccess=${Date.now()}`);
    const { httpStatus, payload } = await mod.runLayerOauthRefreshPush({ layerId: 'layer-x' });
    assert.equal(httpStatus, 503);
    assert.match(payload.detail, /ACCESS_TOKEN/);
  });

  test('runLayerOauthRefreshPush 读取超时 env<30 时夹逼为 30，且记录 begin/token-fetch/fail', async () => {
    const { stateRoot, layerId, gitPushLog } = prepareLayerWithGithubRepo('oauth-timeout-low');
    process.env.TaskApiEndPoint = 'http://127.0.0.1:9/api/tenant/t1/workspace/w1/task/task1/cloud';
    process.env.ACCESS_TOKEN = 'token_for_timeout_low';
    process.env.TRAE_LAYER_GITHUB_OAUTH_FETCH_TIMEOUT_SEC = '5';
    const mod = await import(`./layerGitOauthRefreshPush.mjs?timeoutlow=${Date.now()}`);
    const { httpStatus, payload } = await mod.runLayerOauthRefreshPush({
      layerId,
      targetBranch: 'feature/timeout-low',
    });
    assert.equal(httpStatus, 502);
    assert.match(String(payload?.detail || ''), /拉取 GitHub AccessToken 失败/);
    const log = fs.readFileSync(gitPushLog, 'utf8');
    assert.match(log, /oauth-refresh-push begin/);
    assert.match(log, /oauth-refresh-push token-fetch/);
    assert.match(log, /timeout_sec=30\b/);
    assert.match(log, /oauth-refresh-push fail/);
    assert.doesNotMatch(log, /token_for_timeout_low/);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  });

  test('runLayerOauthRefreshPush 读取超时 env>300 时夹逼为 300', async () => {
    const { stateRoot, layerId, gitPushLog } = prepareLayerWithGithubRepo('oauth-timeout-high');
    process.env.TaskApiEndPoint = 'http://127.0.0.1:9/api/tenant/t1/workspace/w1/task/task1/cloud';
    process.env.ACCESS_TOKEN = 'token_for_timeout_high';
    process.env.TRAE_LAYER_GITHUB_OAUTH_FETCH_TIMEOUT_SEC = '999';
    const mod = await import(`./layerGitOauthRefreshPush.mjs?timeouthigh=${Date.now()}`);
    const { httpStatus } = await mod.runLayerOauthRefreshPush({
      layerId,
      targetBranch: 'feature/timeout-high',
    });
    assert.equal(httpStatus, 502);
    const log = fs.readFileSync(gitPushLog, 'utf8');
    assert.match(log, /timeout_sec=300\b/);
    assert.match(log, /oauth-refresh-push begin/);
    assert.match(log, /oauth-refresh-push token-fetch/);
    assert.match(log, /oauth-refresh-push fail/);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  });

  test('runLayerOauthRefreshPush 超时 env 缺省时使用 120', async () => {
    const { stateRoot, layerId, gitPushLog } = prepareLayerWithGithubRepo('oauth-timeout-default');
    process.env.TaskApiEndPoint = 'http://127.0.0.1:9/api/tenant/t1/workspace/w1/task/task1/cloud';
    process.env.ACCESS_TOKEN = 'token_for_timeout_default';
    delete process.env.TRAE_LAYER_GITHUB_OAUTH_FETCH_TIMEOUT_SEC;
    const mod = await import(`./layerGitOauthRefreshPush.mjs?timeoutdefault=${Date.now()}`);
    const { httpStatus } = await mod.runLayerOauthRefreshPush({
      layerId,
      targetBranch: 'feature/timeout-default',
    });
    assert.equal(httpStatus, 502);
    const log = fs.readFileSync(gitPushLog, 'utf8');
    assert.match(log, /timeout_sec=120\b/);
    assert.match(log, /oauth-refresh-push begin/);
    assert.match(log, /oauth-refresh-push token-fetch/);
    assert.match(log, /oauth-refresh-push fail/);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  });

  test('runLayerOauthFetchTokenFiles 拉取 token 并按仓库写入 .task2app_access_token', async () => {
    const { stateRoot, layerId, repoDir } = prepareLayerWithGithubRepo('oauth-fetch-files');
    const repoDir2 = path.join(path.dirname(repoDir), 'second-repo');
    fs.mkdirSync(repoDir2, { recursive: true });
    assert.equal(spawnSync('git', ['init'], { cwd: repoDir2, encoding: 'utf8' }).status, 0);
    assert.equal(
      spawnSync('git', ['remote', 'add', 'origin', 'https://github.com/acme/second.git'], {
        cwd: repoDir2,
        encoding: 'utf8',
      }).status,
      0,
    );

    const server = http.createServer((req, res) => {
      if (req.method === 'POST' && req.url === '/api/tenant/t1/workspace/w1/task/task1/cloud/server-container-token/task-detail/') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ task: { target_branch: 'feature/demo' } }));
        return;
      }
      if (
        req.method === 'POST' &&
        req.url === '/api/tenant/t1/workspace/w1/task/task1/cloud/server-container-token/layer-github-oauth-access-tokens/'
      ) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(
          JSON.stringify({
            ok: true,
            github_auth_by_repo: {
              'acme/demo': 'token_demo_123',
              'acme/second': 'token_second_456',
            },
          }),
        );
        return;
      }
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ detail: 'not found' }));
    });
    let httpStatus;
    let payload;
    try {
      await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
      const addr = server.address();
      assert(addr && typeof addr === 'object');
      const base = `http://127.0.0.1:${addr.port}`;

      process.env.TaskApiEndPoint = `${base}/api/tenant/t1/workspace/w1/task/task1/cloud`;
      process.env.ACCESS_TOKEN = 'container_access_token';
      const mod = await import(`./layerGitOauthFetchTokenFiles.mjs?fetchfiles=${Date.now()}`);
      const res = await mod.runLayerOauthFetchTokenFiles({
        layerId,
        targetBranch: '',
      });
      httpStatus = res.httpStatus;
      payload = res.payload;
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }

    assert.equal(httpStatus, 200);
    assert.equal(payload?.ok, true);
    assert.match(String(payload?.summary || ''), /已写入/);

    const tokenPath1 = path.join(repoDir, '.task2app_access_token');
    const tokenPath2 = path.join(repoDir2, '.task2app_access_token');
    assert.equal(fs.readFileSync(tokenPath1, 'utf8'), 'token_demo_123\n');
    assert.equal(fs.readFileSync(tokenPath2, 'utf8'), 'token_second_456\n');

    fs.rmSync(stateRoot, { recursive: true, force: true });
  });

  test('runLayerOauthFetchTokenFiles 在 target_branch 缺失时仍可拉取并落盘', async () => {
    const { stateRoot, layerId, repoDir } = prepareLayerWithGithubRepo('oauth-fetch-files-no-branch');
    const requests = [];
    const server = http.createServer((req, res) => {
      requests.push(`${req.method} ${req.url}`);
      if (
        req.method === 'POST' &&
        req.url === '/api/tenant/t1/workspace/w1/task/task1/cloud/server-container-token/layer-github-oauth-access-tokens/'
      ) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(
          JSON.stringify({
            ok: true,
            github_auth_by_repo: {
              'acme/demo': 'token_demo_no_branch',
            },
          }),
        );
        return;
      }
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ detail: 'not found' }));
    });

    let httpStatus;
    let payload;
    try {
      await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
      const addr = server.address();
      assert(addr && typeof addr === 'object');
      const base = `http://127.0.0.1:${addr.port}`;

      process.env.TaskApiEndPoint = `${base}/api/tenant/t1/workspace/w1/task/task1/cloud`;
      process.env.ACCESS_TOKEN = 'container_access_token_no_branch';
      const mod = await import(`./layerGitOauthFetchTokenFiles.mjs?fetchfilesnobranch=${Date.now()}`);
      const res = await mod.runLayerOauthFetchTokenFiles({
        layerId,
        targetBranch: '',
      });
      httpStatus = res.httpStatus;
      payload = res.payload;
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }

    assert.equal(httpStatus, 200);
    assert.equal(payload?.ok, true);
    assert.equal(fs.readFileSync(path.join(repoDir, '.task2app_access_token'), 'utf8'), 'token_demo_no_branch\n');
    assert.deepEqual(
      requests,
      ['POST /api/tenant/t1/workspace/w1/task/task1/cloud/server-container-token/layer-github-oauth-access-tokens/'],
    );

    fs.rmSync(stateRoot, { recursive: true, force: true });
  });
});
