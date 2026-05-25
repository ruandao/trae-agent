import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildHttpAuthFromRepoCredential,
  buildTaskDetailBootstrapError,
  resolveRepoCloneCredential,
} from './bootstrap.mjs';

test('resolveRepoCloneCredential supports canonical repo key match', () => {
  const repoUrl = 'http://localhost:8012/demo/repo-a.git';
  const credRoot = {
    'http://localhost:8012/demo/repo-a': {
      ephemeral_oauth_access_token: 'token-a',
    },
  };
  const got = resolveRepoCloneCredential(credRoot, repoUrl);
  assert.equal(typeof got, 'object');
  assert.equal(got.ephemeral_oauth_access_token, 'token-a');
});

test('resolveRepoCloneCredential falls back to unique path match across host aliases', () => {
  const repoUrl = 'http://localhost:8012/demo/repo-a.git';
  const credRoot = {
    'http://gitlab.aidevpm.com/demo/repo-a.git': {
      ephemeral_oauth_access_token: 'token-alias',
    },
  };
  const got = resolveRepoCloneCredential(credRoot, repoUrl);
  assert.equal(typeof got, 'object');
  assert.equal(got.ephemeral_oauth_access_token, 'token-alias');
});

test('resolveRepoCloneCredential does not guess when multiple credentials share same path', () => {
  const repoUrl = 'http://localhost:8012/demo/repo-a.git';
  const credRoot = {
    'http://gitlab.aidevpm.com/demo/repo-a.git': {
      ephemeral_oauth_access_token: 'token-a',
    },
    'http://another-gitlab.example/demo/repo-a.git': {
      ephemeral_oauth_access_token: 'token-b',
    },
  };
  const got = resolveRepoCloneCredential(credRoot, repoUrl);
  assert.equal(got, null);
});

test('buildHttpAuthFromRepoCredential returns null without password', () => {
  assert.equal(buildHttpAuthFromRepoCredential(null), null);
  assert.equal(buildHttpAuthFromRepoCredential({}), null);
});

test('buildHttpAuthFromRepoCredential returns null when repo path is missing', () => {
  const auth = buildHttpAuthFromRepoCredential({
    ephemeral_oauth_access_token: 'glpat-123',
  });
  assert.equal(auth, null);
});

test('buildHttpAuthFromRepoCredential extracts username from repo path', () => {
  const auth = buildHttpAuthFromRepoCredential(
    {
      ephemeral_oauth_access_token: 'glpat-123',
    },
    'http://localhost:8012/demo/repo-a.git'
  );
  assert.deepEqual(auth, { username: 'demo', password: 'glpat-123' });
});

test('buildHttpAuthFromRepoCredential returns null when repo path cannot be parsed', () => {
  const auth = buildHttpAuthFromRepoCredential(
    {
      ephemeral_oauth_access_token: 'glpat-123',
    },
    'not-a-valid-url'
  );
  assert.equal(auth, null);
});

test('buildTaskDetailBootstrapError renders actionable message for incomplete credentials', () => {
  const err = new Error('HTTP 409 http://api/task-detail/: {"error_code":"REPO_CLONE_CREDENTIALS_INCOMPLETE","detail":"任务仓库克隆凭证不完整","missing_repo_credentials":["http://localhost:8012/demo/repo-a.git"]}');
  const wrapped = buildTaskDetailBootstrapError(err);
  assert.ok(wrapped instanceof Error);
  assert.match(wrapped.message, /未返回完整 repo_clone_credentials/);
  assert.match(wrapped.message, /demo\/repo-a\.git/);
});

test('buildTaskDetailBootstrapError keeps original error when error code is unrelated', () => {
  const err = new Error('HTTP 401 http://api/task-detail/: {"error_code":"TOKEN_ACCESS_INVALID"}');
  const wrapped = buildTaskDetailBootstrapError(err);
  assert.equal(wrapped, err);
});

test('buildTaskDetailBootstrapError parses payload when detail contains braces', () => {
  const err = new Error(
    'HTTP 409 http://api/task-detail/: {"error_code":"REPO_CLONE_CREDENTIALS_INCOMPLETE","detail":"payload has braces {example}","missing_repo_credentials":["http://localhost:8012/demo/repo-a.git"]}'
  );
  const wrapped = buildTaskDetailBootstrapError(err);
  assert.match(wrapped.message, /未返回完整 repo_clone_credentials/);
  assert.match(wrapped.message, /demo\/repo-a\.git/);
});
