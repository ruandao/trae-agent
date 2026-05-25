import test, { mock } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { postJson } from './saasTaskCloud.mjs';

const ENV_KEYS = ['DEBUG_AGENT', 'ONLINE_PROJECT_STATE_ROOT', 'TRACE_ID'];

function snapshotEnv() {
  const out = {};
  for (const k of ENV_KEYS) out[k] = process.env[k];
  return out;
}

function restoreEnv(saved) {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

test('postJson 在 DEBUG_AGENT=true 时记录出站请求与响应完整字段', async () => {
  const saved = snapshotEnv();
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'postjson-debug-'));
  const fetchMock = mock.method(globalThis, 'fetch', async () => {
    return new Response(JSON.stringify({ ok: true, id: 7 }), {
      status: 201,
      headers: {
        'Content-Type': 'application/json',
        'X-Req-Id': 'res-1',
      },
    });
  });
  try {
    process.env.DEBUG_AGENT = 'true';
    process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
    delete process.env.TRACE_ID;

    const data = await postJson('http://127.0.0.1:8080/api/demo', { foo: 'bar' }, 2);
    assert.deepEqual(data, { ok: true, id: 7 });

    const logPath = path.join(stateRoot, 'reqLogs', 'outbound.log');
    const content = fs.readFileSync(logPath, 'utf8');
    assert.match(
      content,
      /DEBUG_AGENT outbound request method=POST url=http:\/\/127\.0\.0\.1:8080\/api\/demo headers=\{"Content-Type":"application\/json"\} body=\{"foo":"bar"\}/,
    );
    assert.match(
      content,
      /DEBUG_AGENT outbound response method=POST url=http:\/\/127\.0\.0\.1:8080\/api\/demo status=201 headers=\{.+\} body=\{"ok":true,"id":7\}/,
    );
  } finally {
    fetchMock.mock.restore();
    restoreEnv(saved);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});

test('postJson 在 DEBUG_AGENT=false 时不记录出站调试字段', async () => {
  const saved = snapshotEnv();
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'postjson-no-debug-'));
  const fetchMock = mock.method(globalThis, 'fetch', async () => {
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  try {
    process.env.DEBUG_AGENT = 'false';
    process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
    delete process.env.TRACE_ID;

    await postJson('http://127.0.0.1:8080/api/ping', { ping: 1 }, 2);

    const logPath = path.join(stateRoot, 'reqLogs', 'outbound.log');
    const content = fs.readFileSync(logPath, 'utf8');
    assert.doesNotMatch(content, /DEBUG_AGENT outbound request method=POST/);
    assert.doesNotMatch(content, /DEBUG_AGENT outbound response method=POST/);
  } finally {
    fetchMock.mock.restore();
    restoreEnv(saved);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});
