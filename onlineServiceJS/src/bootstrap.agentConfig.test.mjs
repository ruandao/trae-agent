import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import test from 'node:test';
import YAML from 'yaml';

import { materializeAgentConfigFile } from './featureParamsEnvToYaml.mjs';

const minimalEnv = {
  TASK_AGENT_MAX_STEPS: '32',
  TASK_MODEL_PROVIDERS_JSON: '[]',
  TASK_TRAE_AGENT_MODEL_PROVIDER: 'openai',
  TASK_TRAE_AGENT_MODEL: 'gpt-4',
  TASK_LAKEVIEW_MODEL_PROVIDER: 'openai',
  TASK_LAKEVIEW_MODEL: 'gpt-4',
};

test('materializeAgentConfigFile writes valid yaml to config path', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'trae-agent-config-'));
  const dest = path.join(dir, 'nested', 'service_config.yaml');
  const written = materializeAgentConfigFile(minimalEnv, {
    configFilePath: () => dest,
    fs,
    yaml: YAML,
    resolveConfig: () => 'agents:\n  trae_agent:\n    max_steps: 32\n',
  });
  assert.equal(written, dest);
  assert.ok(fs.existsSync(dest));
  const parsed = YAML.parse(fs.readFileSync(dest, 'utf8'));
  assert.equal(parsed.agents.trae_agent.max_steps, 32);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('materializeAgentConfigFile rejects invalid yaml before write', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'trae-agent-config-'));
  const dest = path.join(dir, 'service_config.yaml');
  assert.throws(
    () =>
      materializeAgentConfigFile(minimalEnv, {
        configFilePath: () => dest,
        fs,
        yaml: YAML,
        resolveConfig: () => 'agents:\n  bad:\n    - [',
      }),
    (err) => String(err?.message || err).length > 0,
  );
  assert.equal(fs.existsSync(dest), false);
  fs.rmSync(dir, { recursive: true, force: true });
});
