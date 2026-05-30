import test from 'node:test';
import assert from 'node:assert/strict';

import { featureParamsEnvToYaml } from './featureParamsEnvToYaml.mjs';

test('featureParamsEnvToYaml builds trae yaml from env defaults', () => {
  const yaml = featureParamsEnvToYaml({
    TASK_LLM_PROVIDERS_JSON: '[]',
    TASK_AGENT_MODEL: '',
    TASK_AGENT_MODEL_PROVIDER: '',
    TASK_AGENT_MAX_STEPS: '200',
    TASK_SUMMARY_MODEL: '',
    TASK_SUMMARY_MODEL_PROVIDER: '',
  });
  assert.match(yaml, /max_steps: 200/);
  assert.match(yaml, /model: <model>/);
});

test('featureParamsEnvToYaml includes providers and models', () => {
  const yaml = featureParamsEnvToYaml({
    TASK_LLM_PROVIDERS_JSON: JSON.stringify([
      {
        provider: 'openai',
        api_key: 'sk-test',
        base_url: 'https://api.openai.com/v1',
        supported_models: ['gpt-4'],
        use_sub_token: false,
      },
    ]),
    TASK_AGENT_MODEL: 'gpt-4',
    TASK_AGENT_MODEL_PROVIDER: 'openai',
    TASK_AGENT_MAX_STEPS: '100',
    TASK_SUMMARY_MODEL: 'gpt-3.5',
    TASK_SUMMARY_MODEL_PROVIDER: 'openai',
  });
  assert.match(yaml, /max_steps: 100/);
  assert.match(yaml, /gpt-4/);
  assert.match(yaml, /sk-test/);
  assert.match(yaml, /    openai:/);
});
