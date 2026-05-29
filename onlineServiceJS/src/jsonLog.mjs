import { otelTraceIdHex } from './otelTraceId.mjs';
import { resolveTraceId } from './traceId.mjs';

const SERVICE_NAME = 'onlineServiceJS';

/**
 * Emit one JSON log line to stdout for Loki/Promtail (runAll tee).
 * @param {object} [fields]
 * @param {import('express').Request} [fields.req] request-scoped trace (no env fallback)
 * @param {boolean} [fields.use_startup_trace] bootstrap logs: use process.env.TRACE_ID
 */
export function logJson(level, msg, fields = {}) {
  const { req, trace_id, use_startup_trace, ...rest } = fields;
  const traceId = resolveTraceId({
    traceId: trace_id,
    req,
    useStartupEnv: Boolean(use_startup_trace),
  });
  const payload = {
    ts: new Date().toISOString(),
    level: String(level || 'info').toLowerCase(),
    service: SERVICE_NAME,
    msg: String(msg || ''),
    ...rest,
  };
  if (traceId) {
    payload.trace_id = traceId;
    payload.otel_trace_id = otelTraceIdHex(traceId);
  }
  // eslint-disable-next-line no-console
  console.log(JSON.stringify(payload));
}
