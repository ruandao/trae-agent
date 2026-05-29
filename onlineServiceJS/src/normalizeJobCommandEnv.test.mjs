import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeJobCommandEnv } from './normalizeJobCommandEnv.mjs';

test('normalizeJobCommandEnv maps TASK_AGENT_* to TRAE_*', () => {
  const out = normalizeJobCommandEnv({
    TASK_AGENT_MAX_STEPS: '222',
    TASK_AGENT_MODEL: 'gpt-4o-mini',
    TASK_AGENT_MODEL_PROVIDER: 'openai',
  });
  assert.equal(out.TRAE_MAX_STEPS, '222');
  assert.equal(out.TRAE_MODEL, 'gpt-4o-mini');
  assert.equal(out.TRAE_MODEL_PROVIDER, 'openai');
  assert.equal(out.TASK_AGENT_MAX_STEPS, '222');
});

test('normalizeJobCommandEnv does not override explicit TRAE_*', () => {
  const out = normalizeJobCommandEnv({
    TASK_AGENT_MODEL: 'from-task',
    TRAE_MODEL: 'from-trae',
  });
  assert.equal(out.TRAE_MODEL, 'from-trae');
});
