import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildHttpAuthFromRepoCredential,
  buildRepoCloneCredentialsBootstrapError,
  buildTaskDetailBootstrapError,
  fetchBootstrapRepoInputs,
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

test('buildRepoCloneCredentialsBootstrapError renders actionable message for incomplete credentials', () => {
  const err = new Error('HTTP 409 http://api/repo-clone-credentials/: {"error_code":"REPO_CLONE_CREDENTIALS_INCOMPLETE","detail":"任务仓库克隆凭证不完整","missing_repo_credentials":["http://localhost:8012/demo/repo-a.git"]}');
  const wrapped = buildRepoCloneCredentialsBootstrapError(err);
  assert.ok(wrapped instanceof Error);
  assert.match(wrapped.message, /repo-clone-credentials 未返回完整 repo_clone_credentials/);
  assert.match(wrapped.message, /demo\/repo-a\.git/);
});

test('buildRepoCloneCredentialsBootstrapError keeps original error when error code is unrelated', () => {
  const err = new Error('HTTP 401 http://api/repo-clone-credentials/: {"error_code":"TOKEN_ACCESS_INVALID"}');
  const wrapped = buildRepoCloneCredentialsBootstrapError(err);
  assert.equal(wrapped, err);
});

test('buildRepoCloneCredentialsBootstrapError parses payload when detail contains braces', () => {
  const err = new Error(
    'HTTP 409 http://api/repo-clone-credentials/: {"error_code":"REPO_CLONE_CREDENTIALS_INCOMPLETE","detail":"payload has braces {example}","missing_repo_credentials":["http://localhost:8012/demo/repo-a.git"]}'
  );
  const wrapped = buildRepoCloneCredentialsBootstrapError(err);
  assert.match(wrapped.message, /repo-clone-credentials 未返回完整 repo_clone_credentials/);
  assert.match(wrapped.message, /demo\/repo-a\.git/);
});

test('buildTaskDetailBootstrapError delegates to repo credentials bootstrap error mapper', () => {
  const err = new Error(
    'HTTP 409 http://api/repo-clone-credentials/: {"error_code":"REPO_CLONE_CREDENTIALS_INCOMPLETE","detail":"任务仓库克隆凭证不完整","missing_repo_credentials":["http://localhost:8012/demo/repo-a.git"]}'
  );
  const wrapped = buildTaskDetailBootstrapError(err);
  assert.match(wrapped.message, /repo-clone-credentials 未返回完整 repo_clone_credentials/);
});

test('fetchBootstrapRepoInputs fetches task-detail then repo-clone-credentials in order', async () => {
  const originalFetch = global.fetch;
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push(String(url));
    if (String(url).endsWith('/server-container-token/task-detail/')) {
      return {
        ok: true,
        status: 200,
        text: async () =>
          JSON.stringify({
            project_repos: [{ git_repos: ['http://localhost:8012/demo/repo-a.git'] }],
          }),
        headers: new Map(),
      };
    }
    return {
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify({
          repo_clone_credentials: {
            'http://localhost:8012/demo/repo-a.git': {
              ephemeral_oauth_access_token: 'token-a',
            },
          },
        }),
      headers: new Map(),
    };
  };
  try {
    const got = await fetchBootstrapRepoInputs('http://api.example.com', 'access-token', 5);
    assert.deepEqual(got.urls, ['http://localhost:8012/demo/repo-a.git']);
    assert.equal(
      got.credRoot['http://localhost:8012/demo/repo-a.git'].ephemeral_oauth_access_token,
      'token-a'
    );
    assert.equal(calls.length, 2);
    assert.match(calls[0], /server-container-token\/task-detail\/$/);
    assert.match(calls[1], /server-container-token\/repo-clone-credentials\/$/);
  } finally {
    global.fetch = originalFetch;
  }
});

test('fetchBootstrapRepoInputs skips repo-clone-credentials call when no repo urls', async () => {
  const originalFetch = global.fetch;
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push(String(url));
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ project_repos: [] }),
      headers: new Map(),
    };
  };
  try {
    const got = await fetchBootstrapRepoInputs('http://api.example.com', 'access-token', 5);
    assert.deepEqual(got.urls, []);
    assert.deepEqual(got.credRoot, {});
    assert.equal(calls.length, 1);
    assert.match(calls[0], /server-container-token\/task-detail\/$/);
  } finally {
    global.fetch = originalFetch;
  }
});
