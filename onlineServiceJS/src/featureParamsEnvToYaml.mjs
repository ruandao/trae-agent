/**
 * Trae runtime adapter: TASK_* env map → service_config.yaml text.
 */

import path from 'path';

function parseSupportedModels(raw) {
  if (Array.isArray(raw)) {
    return raw.map((item) => String(item || '').trim()).filter(Boolean);
  }
  if (typeof raw === 'string') {
    return raw
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

function buildModelProvidersSection(providersJson) {
  let providers = [];
  try {
    const parsed = JSON.parse(providersJson || '[]');
    providers = Array.isArray(parsed) ? parsed : [];
  } catch {
    providers = [];
  }

  const lines = [];
  for (const item of providers) {
    if (!item || typeof item !== 'object') continue;
    const providerName = String(item.provider || '').trim();
    if (!providerName) continue;
    const apiKey = String(item.api_key || '').trim() || '<api_key>';
    const baseUrl = String(item.base_url || '').trim() || '<base_url>';
    const supportedModels = parseSupportedModels(item.supported_models);
    const supportedModelsYaml = supportedModels.length
      ? supportedModels.map((model) => `            - ${model}`).join('\n')
      : '            - <model>';
    const useSubToken = item.use_sub_token ? 'true' : 'false';
    lines.push(
      `    ${providerName}:\n` +
        `        api_key: ${apiKey}\n` +
        `        provider: ${providerName}\n` +
        `        base_url: ${baseUrl}\n` +
        `        use_sub_token: ${useSubToken}\n` +
        `        supported_models:\n` +
        `${supportedModelsYaml}`
    );
  }

  if (lines.length === 0) {
    return (
      '    <provider>:\n' +
      '        api_key: <api_key>\n' +
      '        provider: <provider>\n' +
      '        base_url: <base_url>\n' +
      '        use_sub_token: false\n' +
      '        supported_models:\n' +
      '            - <model>'
    );
  }
  return lines.join('\n');
}

export function featureParamsEnvToYaml(env) {
  if (!env || typeof env !== 'object') {
    throw new Error('env must be an object');
  }

  const traeModelProvider = String(env.TASK_AGENT_MODEL_PROVIDER || '').trim() || '<provider>';
  const lakeviewModelProvider = String(env.TASK_SUMMARY_MODEL_PROVIDER || '').trim() || '<provider>';
  const traeModel = String(env.TASK_AGENT_MODEL || '').trim() || '<model>';
  const traeMaxSteps = String(env.TASK_AGENT_MAX_STEPS || '').trim() || '200';
  const lakeviewModel = String(env.TASK_SUMMARY_MODEL || '').trim() || '<model>';
  const modelProvidersSection = buildModelProvidersSection(env.TASK_LLM_PROVIDERS_JSON);

  return (
    'agents:\n' +
    '    trae_agent:\n' +
    '        enable_lakeview: true\n' +
    '        model: trae_agent_model\n' +
    `        max_steps: ${traeMaxSteps}\n` +
    '        tools:\n' +
    '            - bash\n' +
    '            - edit_file\n' +
    '            - sequential_thinking\n' +
    '            - complete_task\n' +
    'allow_mcp_servers:\n' +
    '    - playwright\n' +
    'mcp_servers:\n' +
    '    playwright:\n' +
    '        command: npx\n' +
    '        args:\n' +
    '            - "@playwright/mcp@0.0.27"\n' +
    'lakeview:\n' +
    '    model: lakeview_model\n' +
    '\n' +
    'model_providers:\n' +
    `${modelProvidersSection}\n` +
    '\n' +
    'models:\n' +
    '    trae_agent_model:\n' +
    `        model_provider: ${traeModelProvider}\n` +
    `        model: ${traeModel}\n` +
    '        max_tokens: 4096\n' +
    '        temperature: 0.1\n' +
    '        top_p: 1\n' +
    '        top_k: 0\n' +
    '        max_retries: 10\n' +
    '        parallel_tool_calls: true\n' +
    '    lakeview_model:\n' +
    `        model_provider: ${lakeviewModelProvider}\n` +
    `        model: ${lakeviewModel}\n` +
    '        max_tokens: 4096\n' +
    '        temperature: 0.1\n' +
    '        top_p: 1\n' +
    '        top_k: 0\n' +
    '        max_retries: 10\n' +
    '        parallel_tool_calls: true\n'
  );
}

export function resolveAgentConfigFromEnv(env) {
  const runtime = String(process.env.AGENT_RUNTIME || 'trae').toLowerCase();
  if (runtime === 'cursor') {
    throw new Error('AGENT_RUNTIME=cursor not implemented');
  }
  return featureParamsEnvToYaml(env);
}

/**
 * 将 feature-params env 物化为 service_config.yaml（可注入 fs/yaml 便于单测）。
 * @returns {string} 写入的绝对路径
 */
export function materializeAgentConfigFile(env, deps) {
  const {
    configFilePath: configFilePathFn,
    fs: fsMod,
    yaml: yamlMod,
    resolveConfig = resolveAgentConfigFromEnv,
  } = deps;
  const yamlText = resolveConfig(env);
  yamlMod.parse(yamlText);
  const dest = configFilePathFn();
  fsMod.mkdirSync(path.dirname(dest), { recursive: true });
  fsMod.writeFileSync(dest, yamlText, 'utf8');
  return dest;
}
