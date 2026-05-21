import { test, mock, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
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
});
