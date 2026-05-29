import test from 'node:test';
import assert from 'node:assert/strict';

import { otelTraceIdHex } from './otelTraceId.mjs';

test('otelTraceIdHex strips uuid dashes', () => {
  assert.equal(
    otelTraceIdHex('1cd1a1cc-e64d-4325-8b31-caabdd8aa74d'),
    '1cd1a1cce64d43258b31caabdd8aa74d',
  );
});
