import assert from 'node:assert/strict';
import test from 'node:test';

/** 与 bootstrap.mjs 中 isExchangeRefreshForbiddenError 保持同一判定。 */
function isExchangeRefreshForbiddenError(e) {
  const msg = String(e?.message || e || '');
  return /HTTP\s+403\b/i.test(msg) && /refresh-access/i.test(msg);
}

test('isExchangeRefreshForbiddenError detects exchange-refresh 403 hint', () => {
  const err = new Error(
    'HTTP 403 http://api.example/cloud/server-container-token/exchange-refresh/: {"detail":"预埋 AccessToken 仅可用于首次换取 RefreshToken，请使用 server-container-token/refresh-access/ 接口"}',
  );
  assert.equal(isExchangeRefreshForbiddenError(err), true);
});

test('isExchangeRefreshForbiddenError ignores unrelated 403', () => {
  const err = new Error('HTTP 403 http://api.example/forbidden/: {"detail":"forbidden"}');
  assert.equal(isExchangeRefreshForbiddenError(err), false);
});
