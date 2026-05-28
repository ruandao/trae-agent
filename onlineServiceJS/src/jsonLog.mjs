const SERVICE_NAME = 'onlineServiceJS';

/**
 * Emit one JSON log line to stdout for Loki/Promtail (runAll tee).
 */
export function logJson(level, msg, fields = {}) {
  const traceId = String(process.env.TRACE_ID || fields.trace_id || '').trim();
  const payload = {
    ts: new Date().toISOString(),
    level: String(level || 'info').toLowerCase(),
    service: SERVICE_NAME,
    msg: String(msg || ''),
    ...fields,
  };
  if (traceId) payload.trace_id = traceId;
  // eslint-disable-next-line no-console
  console.log(JSON.stringify(payload));
}
