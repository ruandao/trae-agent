import test from 'node:test';
import assert from 'node:assert/strict';

import { logJson } from './jsonLog.mjs';

test('logJson writes trace_id from TRACE_ID env', () => {
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
    assert.equal(payload.service, 'onlineServiceJS');
    assert.equal(payload.msg, 'hello');
  } finally {
    console.log = orig;
    if (prev === undefined) delete process.env.TRACE_ID;
    else process.env.TRACE_ID = prev;
  }
});
