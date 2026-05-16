// @ts-check
import { test } from 'node:test';
import assert from 'node:assert';
import http from 'http';
import {
  postContainerHeartbeatToSaas,
  resetContainerHeartbeatSeqState,
} from './saasTaskCloud.mjs';

function snapshotEnv(keys) {
  const out = {};
  for (const k of keys) {
    out[k] = process.env[k];
  }
  return out;
}

function restoreEnv(saved) {
  for (const k of Object.keys(saved)) {
    const v = saved[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

const KEYS = ['TaskApiEndPoint', 'TASK_API_ENDPOINT', 'tenantId', 'workspaceId', 'taskId', 'ACCESS_TOKEN'];

test('postContainerHeartbeatToSaas：POST .../heartbeat/ 且 body 含 access_token', async () => {
  const saved = snapshotEnv(KEYS);
  /** @type {string} */
  let received = '';
  /** @type {string} */
  let reqUrl = '';
  const server = http.createServer((req, res) => {
    reqUrl = req.url || '';
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      received = Buffer.concat(chunks).toString('utf8');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(
        JSON.stringify({
          status: 'ok',
          task_id: 't1',
          seq: 1,
          ack: 1,
          bidirectional_ok: true,
        }),
      );
    });
  });
  await new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;
  try {
    resetContainerHeartbeatSeqState();
    delete process.env.tenantId;
    delete process.env.workspaceId;
    delete process.env.taskId;
    process.env.TaskApiEndPoint = `http://127.0.0.1:${port}/api/tenant/ta/workspace/ws1/task/td1/cloud`;
    process.env.ACCESS_TOKEN = 'hb-test-token';
    const ok = await postContainerHeartbeatToSaas('ping-msg');
    assert.strictEqual(ok, true);
    assert.ok(reqUrl.includes('/server-container-token/heartbeat/'), `unexpected path: ${reqUrl}`);
    const body = JSON.parse(received);
    assert.strictEqual(body.access_token, 'hb-test-token');
    assert.strictEqual(body.message, 'ping-msg');
    assert.strictEqual(typeof body.seq, 'number');
    assert.ok(body.seq >= 1);
  } finally {
    restoreEnv(saved);
    await new Promise((resolve) => server.close(() => resolve(undefined)));
  }
});
