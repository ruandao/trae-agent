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
});
