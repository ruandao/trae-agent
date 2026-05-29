import { otelTraceIdHex } from './otelTraceId.mjs';

const SERVICE_NAME = 'onlineServiceJS';

/**
 * Emit one JSON log line to stdout for Loki/Promtail (runAll tee).
 */
export function logJson(level, msg, fields = {}) {
  const traceId = String(fields.trace_id || process.env.TRACE_ID || '').trim();
  const payload = {
    ts: new Date().toISOString(),
    level: String(level || 'info').toLowerCase(),
    service: SERVICE_NAME,
    msg: String(msg || ''),
    ...fields,
  };
  if (traceId) {
    payload.trace_id = traceId;
    payload.otel_trace_id = otelTraceIdHex(traceId);
  }
  // eslint-disable-next-line no-console
  console.log(JSON.stringify(payload));
}
