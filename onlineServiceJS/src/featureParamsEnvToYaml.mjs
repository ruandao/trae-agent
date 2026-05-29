/**
 * Trae adapter：将 task2app 下发的 TASK_* env 转为 service_config.yaml。
 */

const TASK_LLM_PROVIDERS_JSON = 'TASK_LLM_PROVIDERS_JSON';
const TASK_AGENT_MODEL = 'TASK_AGENT_MODEL';
const TASK_AGENT_MODEL_PROVIDER = 'TASK_AGENT_MODEL_PROVIDER';
const TASK_AGENT_MAX_STEPS = 'TASK_AGENT_MAX_STEPS';
const TASK_SUMMARY_MODEL = 'TASK_SUMMARY_MODEL';
const TASK_SUMMARY_MODEL_PROVIDER = 'TASK_SUMMARY_MODEL_PROVIDER';

function parseProvidersJson(raw) {
  const text = String(raw || '').trim();
  if (!text) return [];
  const parsed = JSON.parse(text);
  if (!Array.isArray(parsed)) {
    throw new Error('TASK_LLM_PROVIDERS_JSON must be a JSON array');
  }
  return parsed;
}

function buildModelProvidersSection(providers) {
  const lines = [];
  for (const item of providers) {
    if (!item || typeof item !== 'object') continue;
    const providerName = String(item.provider || '').trim();
    if (!providerName) continue;
    const apiKey = String(item.api_key || '').trim() || '<api_key>';
    const baseUrl = String(item.base_url || '').trim() || '<base_url>';
    const useSubToken = item.use_sub_token ? 'true' : 'false';
    let supportedModels = [];
    const rawModels = item.supported_models;
    if (typeof rawModels === 'string') {
      supportedModels = rawModels
        .replace(/\n/g, ',')
        .split(',')
        .map((m) => m.trim())
        .filter(Boolean);
    } else if (Array.isArray(rawModels)) {
      supportedModels = rawModels.map((m) => String(m || '').trim()).filter(Boolean);
    }
    const supportedModelsYaml = supportedModels.length
      ? supportedModels.map((model) => `            - ${model}`).join('\n')
      : '            - <model>';
    lines.push(
      `    ${providerName}:\n` +
        `        api_key: ${apiKey}\n` +
        `        provider: ${providerName}\n` +
        `        base_url: ${baseUrl}\n` +
        `        use_sub_token: ${useSubToken}\n` +
        `        supported_models:\n` +
        `${supportedModelsYaml}`,
    );
  }
  if (lines.length) return lines.join('\n');
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

/**
 * @param {Record<string, string>} env
 * @returns {string}
 */
export function featureParamsEnvToYaml(env) {
  if (!env || typeof env !== 'object') {
    throw new Error('env must be an object');
  }
  const providers = parseProvidersJson(env[TASK_LLM_PROVIDERS_JSON]);
  const agentModelProvider = String(env[TASK_AGENT_MODEL_PROVIDER] || '').trim() || '<provider>';
  const summaryModelProvider = String(env[TASK_SUMMARY_MODEL_PROVIDER] || '').trim() || '<provider>';
  const agentModel = String(env[TASK_AGENT_MODEL] || '').trim() || '<model>';
  const agentMaxSteps = String(env[TASK_AGENT_MAX_STEPS] || '').trim() || '200';
  const summaryModel = String(env[TASK_SUMMARY_MODEL] || '').trim() || '<model>';
  const modelProvidersSection = buildModelProvidersSection(providers);

  return (
    'agents:\n' +
    '    trae_agent:\n' +
    '        enable_lakeview: true\n' +
    '        model: trae_agent_model\n' +
    `        max_steps: ${agentMaxSteps}\n` +
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
    `        model_provider: ${agentModelProvider}\n` +
    `        model: ${agentModel}\n` +
    '        max_tokens: 4096\n' +
    '        temperature: 0.1\n' +
    '        top_p: 1\n' +
    '        top_k: 0\n' +
    '        max_retries: 10\n' +
    '        parallel_tool_calls: true\n' +
    '    lakeview_model:\n' +
    `        model_provider: ${summaryModelProvider}\n` +
    `        model: ${summaryModel}\n` +
    '        max_tokens: 4096\n' +
    '        temperature: 0.1\n' +
    '        top_p: 1\n' +
    '        top_k: 0\n' +
    '        max_retries: 10\n' +
    '        parallel_tool_calls: true\n'
  );
}

/**
 * @param {Record<string, string>} env
 * @returns {string}
 */
export function resolveAgentConfigFromEnv(env) {
  const runtime = String(process.env.AGENT_RUNTIME || 'trae').toLowerCase();
  if (runtime === 'cursor') {
    throw new Error('AGENT_RUNTIME=cursor not implemented');
  }
  return featureParamsEnvToYaml(env);
}

export const FEATURE_PARAMS_ENV_KEYS = {
  TASK_LLM_PROVIDERS_JSON,
  TASK_AGENT_MODEL,
  TASK_AGENT_MODEL_PROVIDER,
  TASK_AGENT_MAX_STEPS,
  TASK_SUMMARY_MODEL,
  TASK_SUMMARY_MODEL_PROVIDER,
};
