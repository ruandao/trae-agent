/**
 * 通用「指令输出」分片流：总览 manifest + 分片拉取 + SSE 推送 seq（类似 HLS/m3u8）。
 * kind + resource_id 区分不同指令（如 clone:layerId、job:jobId）。
 * 分片可带 content_type（text/plain | text/html），HTML 经 htmlSanitize 白名单净化。
 */
import { broadcast } from './sseHub.mjs';
import { sanitizeMachineContainerHtml } from './htmlSanitize.mjs';

const MAX_SEGMENT_CHARS = 4096;
const FLUSH_DEBOUNCE_MS = 380;
/** SSE 附带 content 的最大字符数，避免超大 payload */
const MAX_SSE_SEGMENT_BODY_CHARS = 24000;

/** @typedef {{ text: string, contentType: 'text/plain' | 'text/html' }} PendingBuf */

/** @type {Map<string, { segments: { seq: number, text: string, contentType: 'text/plain' | 'text/html', t: number }[], pending: PendingBuf, nextSeq: number, complete: boolean, completedAt: number | null }>} */
const streams = new Map();

/** @type {Map<string, ReturnType<typeof setTimeout>>} */
const debouncers = new Map();

function streamKey(kind, resourceId) {
  return `${kind}:${resourceId}`;
}

export function validExecStreamKind(kind) {
  return typeof kind === 'string' && /^[a-z][a-z0-9_]{0,31}$/.test(kind);
}

export function validExecStreamResourceId(resourceId) {
  const s = String(resourceId || '').trim();
  if (!s || s.length > 220) return false;
  if (s.includes('..') || s.includes('/') || s.includes('\\')) return false;
  return true;
}

function normalizeContentType(ct) {
  const c = String(ct || '').toLowerCase().trim();
  if (c === 'text/html' || c === 'application/xhtml+xml') return 'text/html';
  return 'text/plain';
}

function prepareChunkText(text, contentType) {
  const s = String(text ?? '');
  if (contentType === 'text/html') return sanitizeMachineContainerHtml(s);
  return s;
}

function notifySegment(kind, resourceId, seq) {
  const st = streams.get(streamKey(kind, resourceId));
  const seg = st?.segments.find((s) => s.seq === seq);
  const contentType = seg?.contentType || 'text/plain';
  const payload = {
    type: 'exec_stream_segment',
    stream: { kind, resource_id: resourceId },
    seq,
    content_type: contentType,
  };
  if (seg && seg.text.length <= MAX_SSE_SEGMENT_BODY_CHARS) {
    payload.content = seg.text;
  }
  broadcast(payload);
}

function notifyComplete(kind, resourceId) {
  broadcast({
    type: 'exec_stream_complete',
    stream: { kind, resource_id: resourceId },
  });
}

export function resetExecStream(kind, resourceId) {
  if (!validExecStreamKind(kind) || !validExecStreamResourceId(resourceId)) return;
  const key = streamKey(kind, resourceId);
  const t = debouncers.get(key);
  if (t) clearTimeout(t);
  debouncers.delete(key);
  streams.set(key, {
    segments: [],
    pending: { text: '', contentType: 'text/plain' },
    nextSeq: 0,
    complete: false,
    completedAt: null,
  });
}

function sealPendingIfTypeMismatch(st, newCt) {
  if (!st.pending.text.length) {
    st.pending.contentType = newCt;
    return;
  }
  if (st.pending.contentType === newCt) return;
  const seq = st.nextSeq++;
  st.segments.push({
    seq,
    text: st.pending.text,
    contentType: st.pending.contentType,
    t: Date.now(),
  });
  st.pending = { text: '', contentType: newCt };
}

function sealMaxChunks(st) {
  const sealed = [];
  while (st.pending.text.length >= MAX_SEGMENT_CHARS) {
    const piece = st.pending.text.slice(0, MAX_SEGMENT_CHARS);
    st.pending.text = st.pending.text.slice(MAX_SEGMENT_CHARS);
    const seq = st.nextSeq++;
    st.segments.push({
      seq,
      text: piece,
      contentType: st.pending.contentType,
      t: Date.now(),
    });
    sealed.push(seq);
  }
  return sealed;
}

function sealAllPending(st) {
  if (!st.pending.text.length) return [];
  const seq = st.nextSeq++;
  st.segments.push({
    seq,
    text: st.pending.text,
    contentType: st.pending.contentType,
    t: Date.now(),
  });
  st.pending = { text: '', contentType: 'text/plain' };
  return [seq];
}

function scheduleDebouncedFlush(kind, resourceId) {
  const key = streamKey(kind, resourceId);
  if (debouncers.has(key)) clearTimeout(debouncers.get(key));
  const st = streams.get(key);
  if (!st || st.complete || !st.pending.text.length) return;
  const t = setTimeout(() => {
    debouncers.delete(key);
    const st2 = streams.get(key);
    if (!st2 || st2.complete || !st2.pending.text.length) return;
    const sealed = sealAllPending(st2);
    sealed.forEach((seq) => notifySegment(kind, resourceId, seq));
  }, FLUSH_DEBOUNCE_MS);
  debouncers.set(key, t);
}

/**
 * 追加输出；达到阈值或 debounce 结束时封包并 SSE 推送 seq。
 * @param {string} kind
 * @param {string} resourceId
 * @param {string} text
 * @param {{ contentType?: string }} [opts]
 * @returns {number[]} 本次同步封包产生的 seq（debounce 中的不算）
 */
export function appendExecStream(kind, resourceId, text, opts = {}) {
  if (!validExecStreamKind(kind) || !validExecStreamResourceId(resourceId)) return [];
  const key = streamKey(kind, resourceId);
  let st = streams.get(key);
  if (!st) {
    resetExecStream(kind, resourceId);
    st = streams.get(key);
  }
  if (!st || st.complete) return [];

  const ct = normalizeContentType(opts.contentType);
  sealPendingIfTypeMismatch(st, ct);
  if (!st.pending.text.length) st.pending.contentType = ct;
  st.pending.text += prepareChunkText(text, ct);

  const sealed = sealMaxChunks(st);
  sealed.forEach((seq) => notifySegment(kind, resourceId, seq));
  scheduleDebouncedFlush(kind, resourceId);
  return sealed;
}

/** 结束流：刷尽 pending，标记 complete，SSE exec_stream_complete */
export function completeExecStream(kind, resourceId) {
  if (!validExecStreamKind(kind) || !validExecStreamResourceId(resourceId)) return;
  const key = streamKey(kind, resourceId);
  const t = debouncers.get(key);
  if (t) clearTimeout(t);
  debouncers.delete(key);

  const st = streams.get(key);
  if (!st || st.complete) return;

  const fromChunks = sealMaxChunks(st);
  fromChunks.forEach((seq) => notifySegment(kind, resourceId, seq));
  const tail = sealAllPending(st);
  tail.forEach((seq) => notifySegment(kind, resourceId, seq));

  st.complete = true;
  st.completedAt = Date.now();
  notifyComplete(kind, resourceId);
}

export function getExecStreamManifest(kind, resourceId) {
  if (!validExecStreamKind(kind) || !validExecStreamResourceId(resourceId)) {
    return null;
  }
  const st = streams.get(streamKey(kind, resourceId));
  const base = {
    version: 1,
    kind,
    resource_id: resourceId,
    segments: [],
    pending_preview_chars: 0,
    complete: false,
    completed_at: null,
  };
  if (!st) {
    return base;
  }
  base.segments = st.segments.map((s) => ({
    seq: s.seq,
    char_length: s.text.length,
    content_type: s.contentType,
    ts: s.t,
  }));
  base.pending_preview_chars = st.pending.text.length;
  base.pending_content_type = st.pending.contentType;
  base.complete = st.complete;
  base.completed_at = st.completedAt;
  return base;
}

export function getExecStreamSegment(kind, resourceId, seq) {
  if (!validExecStreamKind(kind) || !validExecStreamResourceId(resourceId)) return null;
  const n = Number(seq);
  if (!Number.isInteger(n) || n < 0) return null;
  const st = streams.get(streamKey(kind, resourceId));
  if (!st) return null;
  const seg = st.segments.find((s) => s.seq === n);
  if (!seg) return null;
  return {
    version: 1,
    kind,
    resource_id: resourceId,
    seq: seg.seq,
    text: seg.text,
    content_type: seg.contentType,
    encoding: 'utf-8',
    ts: seg.t,
  };
}

/** 兼容旧 clone-log：已提交分片 + 未封包 pending 的完整文本（HTML 分片直接拼接，供复制等） */
export function getExecStreamFullText(kind, resourceId) {
  if (!validExecStreamKind(kind) || !validExecStreamResourceId(resourceId)) return '';
  const st = streams.get(streamKey(kind, resourceId));
  if (!st) return '';
  return st.segments.map((s) => s.text).join('') + st.pending.text;
}
