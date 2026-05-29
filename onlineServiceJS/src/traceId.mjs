/** X-Trace-Id / TRACE_ID resolution for logs and outbound SaaS calls. */

export const TRACE_HEADER = 'X-Trace-Id';

export function startupTraceId() {
  return String(process.env.TRACE_ID || '').trim();
}

export function traceIdFromRequest(req) {
  if (!req) return '';
  const fromReq = String(req.traceId || '').trim();
  if (fromReq) return fromReq;
  const h = req.headers?.[TRACE_HEADER.toLowerCase()] ?? req.headers?.[TRACE_HEADER];
  return String(h || '').trim();
}

/**
 * Logs: explicit trace_id > request trace > startup env (only when useStartupEnv or no req).
 * Request-scoped logs must not fall back to stale process.env.TRACE_ID from container start.
 */
export function resolveTraceId({ traceId, req, useStartupEnv = false } = {}) {
  const explicit = String(traceId || '').trim();
  if (explicit) return explicit;
  const fromReq = traceIdFromRequest(req);
  if (fromReq) return fromReq;
  if (req) return '';
  if (useStartupEnv) return startupTraceId();
  return startupTraceId();
}

/**
 * Outbound SaaS: explicit traceId arg > startup env (bootstrap/heartbeat).
 * Pass req.traceId from HTTP handlers; omit for startup-only calls.
 */
export function resolveOutboundTraceId(traceIdOpt) {
  if (traceIdOpt !== undefined && traceIdOpt !== null) {
    return String(traceIdOpt).trim();
  }
  return startupTraceId();
}

export function traceHeadersForOutbound(traceIdOpt) {
  const headers = { 'Content-Type': 'application/json' };
  const tid = resolveOutboundTraceId(traceIdOpt);
  if (tid) headers[TRACE_HEADER] = tid;
  return headers;
}
