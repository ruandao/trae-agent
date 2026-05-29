import assert from 'node:assert/strict';
import test from 'node:test';

import {
  resolveOutboundTraceId,
  resolveTraceId,
  startupTraceId,
  traceIdFromRequest,
} from './traceId.mjs';

test('traceIdFromRequest prefers req.traceId', () => {
  assert.equal(traceIdFromRequest({ traceId: 'req-trace-abc12345' }), 'req-trace-abc12345');
});

test('traceIdFromRequest reads X-Trace-Id header', () => {
  assert.equal(
    traceIdFromRequest({ headers: { 'x-trace-id': 'header-trace-xyz12345' } }),
    'header-trace-xyz12345',
  );
});

test('resolveTraceId uses startup env only when useStartupEnv', () => {
  const prev = process.env.TRACE_ID;
  process.env.TRACE_ID = 'startup-trace-abc12345';
  try {
    assert.equal(
      resolveTraceId({ useStartupEnv: true }),
      'startup-trace-abc12345',
    );
    assert.equal(resolveTraceId({ req: {} }), '');
    assert.equal(
      resolveTraceId({ req: { traceId: 'page-trace-xyz12345' } }),
      'page-trace-xyz12345',
    );
  } finally {
    if (prev === undefined) delete process.env.TRACE_ID;
    else process.env.TRACE_ID = prev;
  }
});

test('resolveOutboundTraceId prefers explicit over env', () => {
  const prev = process.env.TRACE_ID;
  process.env.TRACE_ID = 'startup-trace-abc12345';
  try {
    assert.equal(resolveOutboundTraceId('forward-trace-xyz12345'), 'forward-trace-xyz12345');
    assert.equal(resolveOutboundTraceId(undefined), 'startup-trace-abc12345');
    assert.equal(resolveOutboundTraceId(''), '');
  } finally {
    if (prev === undefined) delete process.env.TRACE_ID;
    else process.env.TRACE_ID = prev;
  }
});

test('startupTraceId reads TRACE_ID env', () => {
  const prev = process.env.TRACE_ID;
  process.env.TRACE_ID = 'env-only-trace123456';
  try {
    assert.equal(startupTraceId(), 'env-only-trace123456');
  } finally {
    if (prev === undefined) delete process.env.TRACE_ID;
    else process.env.TRACE_ID = prev;
  }
});
