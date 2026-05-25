import fs from 'fs';
import os from 'os';
import path from 'path';
import { EventEmitter } from 'events';
import test from 'node:test';
import assert from 'node:assert/strict';

process.env.ONLINE_SERVICE_JS_SKIP_MAIN = '1';
const { createDebugAgentInboundLoggerMiddleware } = await import('./server.mjs');

function snapshotEnv(keys) {
  const out = {};
  for (const k of keys) out[k] = process.env[k];
  return out;
}

function restoreEnv(saved) {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

function createMockRes() {
  const res = new EventEmitter();
  res.statusCode = 200;
  res._headers = {};
  res.setHeader = (k, v) => {
    res._headers[String(k).toLowerCase()] = v;
  };
  res.getHeaders = () => ({ ...res._headers });
  res.json = (payload) => {
    res.setHeader('content-type', 'application/json');
    res._payload = payload;
    return res;
  };
  res.send = (payload) => {
    res._payload = payload;
    return res;
  };
  return res;
}

const ENV_KEYS = ['DEBUG_AGENT', 'ONLINE_PROJECT_STATE_ROOT', 'ONLINE_SERVICE_JS_SKIP_MAIN'];

test('DEBUG_AGENT=true 时记录入站请求与响应全量字段到 outbound.log', () => {
  const saved = snapshotEnv(ENV_KEYS);
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'online-service-debug-'));
  try {
    process.env.DEBUG_AGENT = 'true';
    process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
    process.env.ONLINE_SERVICE_JS_SKIP_MAIN = '1';
    const middleware = createDebugAgentInboundLoggerMiddleware();
    const req = {
      method: 'POST',
      originalUrl: '/api/demo?foo=1',
      headers: { host: 'localhost', 'x-request-id': 'req-1' },
      body: { hello: 'world' },
    };
    const res = createMockRes();
    middleware(req, res, () => {});
    res.statusCode = 201;
    res.json({ ok: true, id: 123 });
    res.emit('finish');

    const logPath = path.join(stateRoot, 'reqLogs', 'outbound.log');
    const content = fs.readFileSync(logPath, 'utf8');
    assert.match(content, /DEBUG_AGENT inbound request method=POST url=\/api\/demo\?foo=1/);
    assert.match(content, /headers=\{"host":"localhost","x-request-id":"req-1"\}/);
    assert.match(content, /body=\{"hello":"world"\}/);
    assert.match(content, /response_status=201/);
    assert.match(content, /response_headers=\{"content-type":"application\/json"\}/);
    assert.match(content, /response_body=\{"ok":true,"id":123\}/);
  } finally {
    restoreEnv(saved);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});

test('DEBUG_AGENT=false 时不记录入站调试日志', () => {
  const saved = snapshotEnv(ENV_KEYS);
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'online-service-debug-'));
  try {
    process.env.DEBUG_AGENT = 'false';
    process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
    process.env.ONLINE_SERVICE_JS_SKIP_MAIN = '1';
    const middleware = createDebugAgentInboundLoggerMiddleware();
    const req = {
      method: 'GET',
      originalUrl: '/api/ping',
      headers: { host: 'localhost' },
      body: null,
    };
    const res = createMockRes();
    middleware(req, res, () => {});
    res.statusCode = 204;
    res.send('');
    res.emit('finish');

    const logPath = path.join(stateRoot, 'reqLogs', 'outbound.log');
    assert.equal(fs.existsSync(logPath), false);
  } finally {
    restoreEnv(saved);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});
