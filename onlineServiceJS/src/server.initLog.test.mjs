import test from 'node:test';
import assert from 'node:assert/strict';

process.env.ONLINE_SERVICE_JS_SKIP_MAIN = '1';
const { main } = await import('./server.mjs');

test('启动时会触发 init.log 写入', async () => {
  let appendCalled = 0;
  let appendPayload = null;

  await main({
    appendInitLog: (payload) => {
      appendCalled += 1;
      appendPayload = payload;
    },
    runBootstrapTokenExchangeOnlyFn: async () => ({ skipped: true }),
    startSsePingLoop: () => {},
    stopAfterBootstrapTokenExchangeOnly: true,
  });

  assert.equal(appendCalled, 1);
  assert.equal(typeof appendPayload, 'object');
  assert.equal(appendPayload.port, String(process.env.PORT || '8765'));
});

test('init.log 写入抛错也不阻断启动流程', async () => {
  let bootstrapCalled = false;

  await main({
    appendInitLog: () => {
      throw new Error('disk full');
    },
    runBootstrapTokenExchangeOnlyFn: async () => {
      bootstrapCalled = true;
      return { skipped: true };
    },
    startSsePingLoop: () => {},
    stopAfterBootstrapTokenExchangeOnly: true,
  });

  assert.equal(bootstrapCalled, true);
});

test('init.log 返回失败状态时会打印错误且不阻断启动流程', async () => {
  let bootstrapCalled = false;
  const errors = [];
  const origError = console.error;
  console.error = (...args) => {
    errors.push(args.map((x) => String(x)).join(' '));
  };
  try {
    await main({
      appendInitLog: () => ({ ok: false, error: new Error('disk full') }),
      runBootstrapTokenExchangeOnlyFn: async () => {
        bootstrapCalled = true;
        return { skipped: true };
      },
      startSsePingLoop: () => {},
      stopAfterBootstrapTokenExchangeOnly: true,
    });
  } finally {
    console.error = origError;
  }

  assert.equal(bootstrapCalled, true);
  assert.ok(errors.some((line) => line.includes('init.log append error')));
  assert.ok(errors.some((line) => line.includes('disk full')));
});
