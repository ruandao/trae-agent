/**
 * 与 task2app/Saas_project/skillList/machine_container.md §7.2–7.4 对齐的 HTML 白名单净化。
 */
import sanitizeHtml from 'sanitize-html';

const ALLOWED_TAGS = [
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'p',
  'div',
  'span',
  'strong',
  'b',
  'em',
  'i',
  'ul',
  'ol',
  'li',
  'blockquote',
  'table',
  'thead',
  'tbody',
  'tr',
  'th',
  'td',
  'br',
  'hr',
  'code',
  'pre',
];

const SANITIZE_OPTS = {
  allowedTags: ALLOWED_TAGS,
  allowedAttributes: {
    '*': ['class', 'colspan', 'rowspan'],
    span: ['class', 'data-value', 'data-action', 'data-param', 'data-group'],
    div: ['class', 'data-value', 'data-action', 'data-param', 'data-group'],
    p: ['class'],
    td: ['class', 'colspan', 'rowspan'],
    th: ['class', 'colspan', 'rowspan'],
    table: ['class'],
    code: ['class'],
    pre: ['class'],
  },
  allowedSchemes: [],
  allowVulnerableTags: false,
  disallowedTagsMode: 'discard',
};

/**
 * 可放入 GET /api/ui/agent-render-hints 的 machine-readable 白名单，
 * 与上方 SANITIZE_OPTS 一致，供富文本编辑器配置与导出校验。
 */
export function getMachineContainerV1AllowlistSpec() {
  return {
    profile_id: 'machine_container_v1',
    doc_ref: 'task2app/Saas_project/skillList/machine_container.md §7.2–7.4',
    tags: [...ALLOWED_TAGS],
    attributes: {
      '*': [...SANITIZE_OPTS.allowedAttributes['*']],
      span: [...SANITIZE_OPTS.allowedAttributes.span],
      div: [...SANITIZE_OPTS.allowedAttributes.div],
      p: [...SANITIZE_OPTS.allowedAttributes.p],
      td: [...SANITIZE_OPTS.allowedAttributes.td],
      th: [...SANITIZE_OPTS.allowedAttributes.th],
      table: [...SANITIZE_OPTS.allowedAttributes.table],
      code: [...SANITIZE_OPTS.allowedAttributes.code],
      pre: [...SANITIZE_OPTS.allowedAttributes.pre],
    },
    allowed_url_schemes: [...SANITIZE_OPTS.allowedSchemes],
    interactive_span_classes: {
      doc_ref: 'machine_container.md §7.4',
      opt_radio: '单选',
      opt_checkbox: '多选',
      'action-btn': '按钮（data-action / data-param）',
    },
    forbidden_summary:
      'script、on*、javascript:、iframe、object、embed、form、input 等；全文见 machine_container.md §7.2',
  };
}

export function sanitizeMachineContainerHtml(html) {
  const raw = String(html ?? '');
  if (!raw.trim()) return '';
  return sanitizeHtml(raw, SANITIZE_OPTS);
}
