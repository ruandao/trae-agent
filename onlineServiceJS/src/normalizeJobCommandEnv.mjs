/** 将 task2app 下发的 TASK_AGENT_* 映射为 Trae 运行时识别的 TRAE_*。 */

const TASK_TO_TRAE = {
  TASK_AGENT_MAX_STEPS: 'TRAE_MAX_STEPS',
  TASK_AGENT_MODEL: 'TRAE_MODEL',
  TASK_AGENT_MODEL_PROVIDER: 'TRAE_MODEL_PROVIDER',
};

/**
 * @param {Record<string, unknown> | null | undefined} commandEnv
 * @returns {Record<string, string>}
 */
export function normalizeJobCommandEnv(commandEnv) {
  if (!commandEnv || typeof commandEnv !== 'object') {
    return {};
  }
  const out = {};
  for (const [key, value] of Object.entries(commandEnv)) {
    if (value == null) continue;
    out[String(key)] = String(value);
  }
  for (const [taskKey, traeKey] of Object.entries(TASK_TO_TRAE)) {
    const taskVal = out[taskKey];
    if (taskVal == null || taskVal === '') continue;
    if (out[traeKey] == null || out[traeKey] === '') {
      out[traeKey] = taskVal;
    }
  }
  return out;
}
