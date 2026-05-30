/**
 * Job payload env uses TASK_AGENT_* keys; trae-cli still reads TRAE_*.
 */
export function normalizeJobCommandEnv(env) {
  if (!env || typeof env !== 'object') {
    return {};
  }
  const out = { ...env };
  const mapping = {
    TASK_AGENT_MAX_STEPS: 'TRAE_MAX_STEPS',
    TASK_AGENT_MODEL: 'TRAE_MODEL',
    TASK_AGENT_MODEL_PROVIDER: 'TRAE_MODEL_PROVIDER',
  };
  for (const [from, to] of Object.entries(mapping)) {
    if (Object.prototype.hasOwnProperty.call(out, from) && !Object.prototype.hasOwnProperty.call(out, to)) {
      out[to] = out[from];
    }
  }
  return out;
}
