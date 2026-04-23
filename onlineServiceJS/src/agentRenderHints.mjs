/**
 * Agent 步骤/响应字段 → 前端富文本呈现策略（表驱动）。
 * HTML 子集与净化约定见 task2app/Saas_project/skillList/machine_container.md §7。
 */

import { getMachineContainerV1AllowlistSpec } from './htmlSanitize.mjs';

const HINTS = {
  version: 1,
  doc_ref: 'machine_container.md §7（text/html 白名单标签与 data-* 交互）',
  html_sanitize_profile: 'machine_container_v1',
  /** 呈现模式说明：供前端与文档生成器对齐 */
  presentation_modes: {
    plain_cell: {
      description: '单行或多行纯文本，HTML 转义后写入 td',
      mime: 'text/plain',
    },
    preformatted: {
      description: '等宽 pre 块，适合长文本/终端输出',
      mime: 'text/plain',
    },
    rich_iframe: {
      description:
        '按 MIME 在沙箱 iframe 中展示：text/plain 转义进 pre；text/html 为富文本片段（须符合同响应内 rich_text_editor.html_allowlist，或由 exec-stream 等路径经 sanitizeMachineContainerHtml 净化）',
      mime: ['text/plain', 'text/html', 'application/xhtml+xml'],
      editor_html_contract: 'rich_text_editor',
    },
    json_pre: {
      description: 'JSON 格式化后写入 pre',
      mime: 'application/json',
    },
    usage_line: {
      description: 'Token 用量格式化为一行可读文本',
    },
    tool_names_join: {
      description: '将 tool_calls[].name 逗号连接为一行纯文本',
      mime: 'text/plain',
    },
  },
  /**
   * 按顺序渲染的「标量」行；每行在满足 when 时输出一行表格。
   * value_path / mime_path 为 agent_step 根上的点路径。
   */
  step_rows: [
    {
      id: 'state',
      label: '状态',
      when: { always: true },
      value_path: 'state',
      presentation: 'plain_cell',
      value_map: 'state_label',
      coalesce_empty: '—',
    },
    {
      id: 'timestamp',
      label: '时间',
      when: { always: true },
      value_path: 'timestamp',
      presentation: 'plain_cell',
      coalesce_empty: '—',
    },
    {
      id: 'lakeview_summary',
      label: 'Lakeview 摘要',
      when: { path: 'lakeview_summary', non_empty: true },
      value_path: 'lakeview_summary',
      presentation: 'preformatted',
    },
    {
      id: 'delivery_summary',
      label: '交付摘要',
      when: { path: 'delivery_summary', non_empty: true },
      value_path: 'delivery_summary',
      presentation: 'preformatted',
    },
    {
      id: 'thought',
      label: '思考',
      when: { path: 'thought', non_empty: true },
      value_path: 'thought',
      presentation: 'preformatted',
    },
    {
      id: 'llm_reply',
      label: 'LLM 回复',
      when: { path: 'llm_response.content', non_empty: true },
      value_path: 'llm_response.content',
      mime_path: 'llm_response.content_type',
      default_mime: 'text/plain',
      presentation: 'rich_iframe',
    },
    {
      id: 'llm_usage',
      label: '本步 Token',
      when: { path: 'llm_response.usage' },
      usage_path: 'llm_response.usage',
      presentation: 'usage_line',
    },
  ],
  /** 在 tool_expansion 之后渲染，与旧版表格顺序一致 */
  tail_rows: [
    {
      id: 'reflection',
      label: '反思',
      when: { path: 'reflection', non_empty: true },
      value_path: 'reflection',
      presentation: 'preformatted',
    },
    {
      id: 'error',
      label: '错误',
      when: { path: 'error', non_empty: true },
      value_path: 'error',
      presentation: 'preformatted',
    },
  ],
  /**
   * 工具调用展开：对每个 tool_call 追加多行；与 tool_results 按 call_id 关联。
   */
  tool_expansion: {
    when: { path: 'tool_calls', min_length: 1 },
    summary_row: {
      label: '工具',
      names_path: 'tool_calls',
      presentation: 'tool_names_join',
    },
    per_call: {
      arg_label_template: '参数 · {name}',
      arg_source_path: 'arguments',
      arg_presentation: 'json_pre',
      result_label_template: '结果 · {name}',
      result_value_path: 'result',
      result_error_path: 'error',
      result_mime_path: 'content_type',
      result_default_mime: 'text/plain',
      result_presentation: 'rich_iframe',
    },
  },
  /**
   * 富文本编辑器侧可消费的声明：导出 HTML 时仅使用 html_allowlist；
   * 与 presentation_modes.rich_iframe / machine_container §7 / htmlSanitize 同源。
   */
  rich_text_editor: {
    description:
      '与 Trae 步骤/任务详情富文本链路对齐的编辑器契约：产出片段、MIME、白名单与呈现方式。',
    intended_use: [
      '配置编辑器 allowedTags / allowedAttributes（或导出前校验）',
      '与 rich_iframe 字段联用：content_type 为 text/html 时正文为 HTML 片段，勿包含 html/head/body',
      'exec-stream 等服务端路径在 text/html 时已按同 profile 净化；落盘步骤 JSON 建议在写入前净化或约束模型',
    ],
    spec_alignment: {
      human_doc: 'machine_container.md §7.1–7.5',
      server_impl: 'onlineServiceJS/src/htmlSanitize.mjs — sanitizeMachineContainerHtml',
      ui_wrapper: 'onlineServiceJS/static/index.html — buildExecRichSrcdoc(.rich-chunk)',
    },
    content_type: {
      html_fragment: ['text/html', 'application/xhtml+xml'],
      markdown_body: ['text/markdown', 'text/md'],
      plain: ['text/plain'],
    },
    export_shape: {
      form: 'html_fragment',
      rule: '仅内联片段；消费者包进 iframe srcdoc 并加展示用 CSS，不依赖编辑器自带 document 外壳',
    },
  },
};

export function getAgentRenderHints() {
  const out = JSON.parse(JSON.stringify(HINTS));
  out.rich_text_editor = {
    ...out.rich_text_editor,
    html_allowlist: getMachineContainerV1AllowlistSpec(),
  };
  return out;
}
