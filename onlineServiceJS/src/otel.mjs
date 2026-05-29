import { context, trace, SpanKind, TraceFlags } from '@opentelemetry/api';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-grpc';
import { Resource } from '@opentelemetry/resources';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { SemanticResourceAttributes } from '@opentelemetry/semantic-conventions';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';

import { otelTraceIdHex, randomSpanIdHex } from './otelTraceId.mjs';

let active = false;
let tracer = null;
let provider = null;

function grpcEndpoint(raw) {
  const s = String(raw || '').trim().replace(/^https?:\/\//, '');
  return s.includes(':') ? s : `${s}:4317`;
}

/** No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset. */
export function initOtel(serviceName = 'onlineServiceJS') {
  const endpoint = String(process.env.OTEL_EXPORTER_OTLP_ENDPOINT || '').trim();
  if (!endpoint) return () => {};
  const svc = String(process.env.OTEL_SERVICE_NAME || serviceName).trim() || serviceName;
  provider = new NodeTracerProvider({
    resource: new Resource({
      [SemanticResourceAttributes.SERVICE_NAME]: svc,
    }),
  });
  const exporter = new OTLPTraceExporter({ url: grpcEndpoint(endpoint) });
  provider.addSpanProcessor(new BatchSpanProcessor(exporter));
  provider.register();
  tracer = trace.getTracer(`${svc}.http`);
  active = true;
  return async () => {
    await provider?.shutdown();
  };
}

export function startHttpSpan(req, externalTraceId) {
  const tid = String(externalTraceId || '').trim();
  if (!tid || !active || !tracer) {
    return { ctx: context.active(), end: () => {} };
  }
  const traceId = otelTraceIdHex(tid);
  const spanId = randomSpanIdHex();
  const parentCtx = trace.setSpanContext(context.active(), {
    traceId,
    spanId,
    isRemote: true,
    traceFlags: TraceFlags.SAMPLED,
  });
  const span = tracer.startSpan(
    `${req.method} ${req.originalUrl || req.url}`,
    {
      kind: SpanKind.SERVER,
      attributes: {
        'http.method': req.method,
        'http.target': req.originalUrl || req.url,
        trace_id: tid,
      },
    },
    parentCtx,
  );
  const ctx = trace.setSpan(context.active(), span);
  return {
    ctx,
    end: () => span.end(),
  };
}
