import assert from 'node:assert/strict';
import test from 'node:test';

import { buildHttpAuthFromRepoCredential, resolveRepoCloneCredential } from './bootstrap.mjs';

test('resolveRepoCloneCredential supports canonical repo key match', () => {
  const repoUrl = 'http://localhost:8012/demo/repo-a.git';
  const credRoot = {
    'http://localhost:8012/demo/repo-a': {
      ephemeral_git_remote_username: 'demo-user',
      ephemeral_git_http_password: 'token-a',
    },
  };
  const got = resolveRepoCloneCredential(credRoot, repoUrl);
  assert.equal(typeof got, 'object');
  assert.equal(got.ephemeral_git_remote_username, 'demo-user');
});

test('buildHttpAuthFromRepoCredential returns null without password', () => {
  assert.equal(buildHttpAuthFromRepoCredential(null), null);
  assert.equal(
    buildHttpAuthFromRepoCredential({
      ephemeral_git_remote_username: 'demo-user',
    }),
    null
  );
});

test('buildHttpAuthFromRepoCredential falls back username to oauth2', () => {
  const auth = buildHttpAuthFromRepoCredential({
    ephemeral_git_http_password: 'glpat-123',
  });
  assert.deepEqual(auth, { username: 'oauth2', password: 'glpat-123' });
});
