// @ts-check
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

test('runLayerGithubOauthAccessPush: localhost GitLab + oauth_auth_by_repo 应尝试推送', async () => {
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'oauth-push-gitlab-local-'));
  process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
  const layerId = 'oauth-push-gitlab-local-layer';
  const layerDir = path.join(stateRoot, 'layers', layerId);
  const repoDir = path.join(layerDir, 'somanyad');
  fs.mkdirSync(repoDir, { recursive: true });
  fs.writeFileSync(
    path.join(layerDir, 'layer_meta.json'),
    JSON.stringify({ layer_id: layerId, kind: 'workspace' }),
  );
  assert.equal(spawnSync('git', ['init'], { cwd: repoDir, encoding: 'utf8' }).status, 0);
  assert.equal(
    spawnSync('git', ['remote', 'add', 'origin', 'http://localhost:8012/ljy/somanyad.git'], {
      cwd: repoDir,
      encoding: 'utf8',
    }).status,
    0,
  );
  assert.equal(
    spawnSync('git', ['config', 'user.email', 'e2e@test'], { cwd: repoDir, encoding: 'utf8' }).status,
    0,
  );
  assert.equal(
    spawnSync('git', ['config', 'user.name', 'e2e'], { cwd: repoDir, encoding: 'utf8' }).status,
    0,
  );
  fs.writeFileSync(path.join(repoDir, 'hello.js'), "console.log('hello world')\n");
  assert.equal(spawnSync('git', ['add', 'hello.js'], { cwd: repoDir, encoding: 'utf8' }).status, 0);
  assert.equal(
    spawnSync('git', ['commit', '-m', 'e2e push'], { cwd: repoDir, encoding: 'utf8' }).status,
    0,
  );

  const { runLayerGithubOauthAccessPush } = await import('./layerGitOauthPush.mjs');
  const { httpStatus, payload } = await runLayerGithubOauthAccessPush({
    layerId,
    targetBranch: 'feature/e2e-local-gitlab',
    oauthAuthByRepo: {
      'http://localhost:8012/ljy/somanyad': {
        provider: 'gitlab',
        access_token: 'glpat-test-token-not-real',
      },
    },
  });

  const repos = payload?.github_oauth_multirepo?.repos;
  assert.ok(Array.isArray(repos) && repos.length === 1, '应处理 1 个 git 根目录');
  const row = repos[0];
  assert.equal(row.provider, 'gitlab');
  assert.equal(row.github_slug, 'ljy/somanyad');
  assert.notEqual(row.detail, 'remote 无法识别且 oauth_auth_by_repo 无匹配项，已跳过 OAuth 推送');
  assert.match(
    String(row.detail || ''),
    /push|auth|401|403|could not|rejected|timeout|超时|fatal|Repository/i,
    '应执行 git push（成功或远端拒绝），而非跳过',
  );

  fs.rmSync(stateRoot, { recursive: true, force: true });
});
