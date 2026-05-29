import test from 'node:test';
import assert from 'node:assert/strict';

import { logJson } from './jsonLog.mjs';
import { otelTraceIdHex } from './otelTraceId.mjs';

test('logJson writes trace_id and otel_trace_id from TRACE_ID env', () => {
  const prev = process.env.TRACE_ID;
  process.env.TRACE_ID = 'trace-node-test1234';
  const lines = [];
  const orig = console.log;
  console.log = (line) => lines.push(String(line));
  try {
    logJson('info', 'hello');
    assert.equal(lines.length, 1);
    const payload = JSON.parse(lines[0]);
    assert.equal(payload.trace_id, 'trace-node-test1234');
    assert.equal(payload.otel_trace_id, otelTraceIdHex('trace-node-test1234'));
    assert.equal(payload.service, 'onlineServiceJS');
    assert.equal(payload.msg, 'hello');
  } finally {
    console.log = orig;
    if (prev === undefined) delete process.env.TRACE_ID;
    else process.env.TRACE_ID = prev;
  }
});

test('logJson prefers req.traceId over stale TRACE_ID env', () => {
  const prev = process.env.TRACE_ID;
  process.env.TRACE_ID = 'startup-trace-abc12345';
  const lines = [];
  const orig = console.log;
  console.log = (line) => lines.push(String(line));
  try {
    logJson('info', 'forwarded', { req: { traceId: 'page-trace-xyz12345' } });
    const payload = JSON.parse(lines[0]);
    assert.equal(payload.trace_id, 'page-trace-xyz12345');
  } finally {
    console.log = orig;
    if (prev === undefined) delete process.env.TRACE_ID;
    else process.env.TRACE_ID = prev;
  }
});

test('logJson use_startup_trace reads TRACE_ID env', () => {
  const prev = process.env.TRACE_ID;
  process.env.TRACE_ID = 'bootstrap-trace-abc123';
  const lines = [];
  const orig = console.log;
  console.log = (line) => lines.push(String(line));
  try {
    logJson('error', 'reachability_failed', { use_startup_trace: true, detail: 'x' });
    const payload = JSON.parse(lines[0]);
    assert.equal(payload.trace_id, 'bootstrap-trace-abc123');
    assert.equal(payload.msg, 'reachability_failed');
  } finally {
    console.log = orig;
    if (prev === undefined) delete process.env.TRACE_ID;
    else process.env.TRACE_ID = prev;
  }
});
