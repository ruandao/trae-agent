# Trae Online Service Skill（Node / onlineServiceJS）

面向自动化代理与工具的说明：如何通过 HTTP 调用本仓库中的 **onlineServiceJS**（Express / Node.js），推送配置、克隆远程仓库到可写层、在分层工作区中执行 `trae-cli run`（或 shell）、浏览层内文件与任务，并通过 SSE 订阅事件。

路径与字段名与任务云及历史约定对齐，便于编排与脚本复用；未列出的行为以 `src/server.mjs` 及对应模块实现为准。

## 环境变量

| 变量 | 说明 |
|------|------|
| `ACCESS_TOKEN` | **必填**。所有受保护接口须携带与此相同的令牌（查询参数或 Header）。未配置时受保护路由会拒绝访问。 |
| `REPO_ROOT` | 可选。Trae 仓库根目录，默认 **onlineServiceJS 的上一级目录**。 |
| `ONLINE_PROJECT_STATE_ROOT` | 可选。运行时状态根目录（其下含 `runtime`、`logs`、`reqLogs` 等），默认 `{REPO_ROOT}/onlineProject_state`。 |
| `TRAE_VENV` | 可选。含 `trae-cli` 的虚拟环境根路径，默认 `{REPO_ROOT}/.venv`；通过 `{TRAE_VENV}/bin/trae-cli`（及同目录 `python` / `python3` 的 `-m trae_agent.cli`）解析命令。 |
| `TRAE_CLI` | 可选。若设置，则 **`command_kind=trae`** 时直接以该可执行文件运行，参数形如：`<命令文本> --working-dir=<层目录>`（不再拼接 `--config-file`，由可执行文件自身或环境决定）。 |
| `ONLINE_PROJECT_LAYERS` | 可选。可写层根目录，默认 `{REPO_ROOT}/onlineProject_state/layers`。 |
| `PORT` | 可选。HTTP 监听端口，默认 **8765**。 |

容器换票、引导克隆等仍可使用：`TaskApiEndPoint`、`BusinessApiEndPoint`、`BUSINESS_API_ENDPOINT`、`tenantId`、`workspaceId`、`taskId`、`ACCESS_TOKEN`（与任务云约定一致）。**启动就绪日志**：标准输出含 **`[onlineServiceJS] server listening on http://0.0.0.0:<PORT>`**，供编排检测。

运行时与任务行为还可通过环境变量调节（见 `src/jobsRuntime.mjs`、`src/bootstrap.mjs`）；克隆相关常见有 `GIT_CLONE_TIMEOUT_SEC` 等（以代码为准）。

配置文件固定路径：`onlineProject_state/runtime/service_config.yaml`（由 API 写入；内容与仓库根目录 `trae_config.yaml.example` 同结构）。任务状态持久化：`onlineProject_state/runtime/jobs_state.json`。Docker 镜像内示例：`/app/trae_config.yaml.example`。

## 推荐调用顺序

1. `POST /api/config`（或 `POST /api/config/raw`）推送有效 YAML。
2. 若需真实执行 Trae：确保存在 **`trae-cli`**（`TRAE_VENV` 或 `TRAE_CLI`）；否则 Node 版会对 **`command_kind=trae`** 走**占位 stub**（见下文「功能与限制」）。
3. `POST /api/repos/clone` 将远程仓库克隆到**新的**可写层（需系统已安装 `git`）。
4. 再 `POST /api/jobs` 创建任务（须满足任务门控；且 **必须** 提供 `parent_job_id` 或 `repo_layer_id` 之一，见下文）。

## 公开端点（无需令牌）

| 路径 | 说明 |
|------|------|
| `GET /skill.md` | 本 Skill 文档（Markdown，`text/markdown; charset=utf-8`）。 |

**说明**：本服务**不提供** `GET /docs`、`GET /openapi.json`；请以本文档与 `src/server.mjs` 为准。

## 受保护 API 的认证

以下任一方式：

- 查询参数：`?access_token=<ACCESS_TOKEN>`
- 请求头：`X-Access-Token: <ACCESS_TOKEN>`

## 配置

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/config` | `multipart/form-data`，字段名 `file`，内容为 YAML。校验通过后写入 `service_config.yaml`。 |
| `POST` | `/api/config/raw` | 查询参数 `yaml=`（仅适合较短内容；大文件请用 multipart）。 |
| `GET` | `/api/config` | 返回 `path` 与 `yaml` 文本；若尚未推送则 404。 |

## 远程仓库克隆

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/repos/clone` | JSON：`url`（必填），`branch`、`depth`、`ssh_identity_file`、`ephemeral_ssh_private_key`、`parent_layer_id`（可选）。在新可写层目录内执行 `git clone`；成功返回 202 Accepted，响应体含 `accepted: true`、`status: "queued"`、`layer_id`、`layer_path`、`queue_position`。失败时 400，响应体可含 `exit_code`、`detail`。克隆过程与结果可通过 SSE 推送（见下文）。 |
| `GET` | `/api/repos/clone-status/:layer_id` | 查询克隆操作状态，返回 `layer_id` 和状态信息。 |

- **`ssh_identity_file`**（可选）：服务器本机可读 SSH **私钥文件路径**（绝对或相对路径经 `path.resolve`）。存在且为普通文件时，设置 `GIT_SSH_COMMAND`；HTTPS 远程会转为 `git@host:…` 以便走 SSH。路径无效时 **400**。
- **`ephemeral_ssh_private_key`**（可选）：单次请求 PEM/OpenSSH 私钥文本。仅当内容**同时**含 `-----BEGIN…PRIVATE KEY-----` 与 `-----END…KEY-----` 时才视为有效；**否则忽略**（避免 UI/localStorage 残留非 PEM 文本时误把公开 HTTPS 转成 `git@` 导致克隆失败）。有效时行为同前（临时文件、`GIT_SSH_COMMAND`、HTTPS 可转 SSH）。
- **`branch` / `depth`**：传入时分别对应 `git clone --branch`、`--depth`（正整数）；不传则由 `git` 默认行为决定。
- **`parent_layer_id`**（可选）：写入新层 `layer_meta.json` 的父指针。
- 容器引导多仓克隆（`task_api_bootstrap`）逻辑在 `src/bootstrap.mjs`；持有 PEM 时使用临时密钥与 `GIT_SSH_COMMAND`（细节以代码为准）。

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/repos/reclone` | JSON：`repo_url`（必填）、`ephemeral_ssh_private_key`（可选）。在引导层（或首个含 git 的层）内删除对应子目录并重新 `git clone`。 |

## 执行流（Exec Streams）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/exec-streams/:kind/:resourceId/manifest` | 通用执行流总览，返回分片列表（JSON）。`kind` 和 `resourceId` 需符合验证规则。 |
| `GET` | `/api/exec-streams/:kind/:resourceId/segments/:seq` | 获取执行流指定序列的分片，返回分片内容（JSON）。 |

## 任务门控

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/requirements/task-gate` | 返回 `clone_done`（布尔）：可写层根下是否**至少存在一个含 `.git` 的层**。 |

## 任务（指令）

### 创建与查询

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/jobs` | JSON：`command`（必填）、`command_kind`（`trae` \| `shell`，默认 `trae`）、`parent_job_id`（可选）、`repo_layer_id`（可选）、`git_branch`（可选）、`env`（可选）。 |
| `GET` | `/api/jobs` | 返回 `{ "jobs": [ ... ] }`。字段含 `git_destructive_locked`：**当前恒为 `false`**（未实现基线锁定）。 |
| `GET` | `/api/jobs/{job_id}` | 单条任务详情。 |
| `GET` | `/api/jobs/{job_id}/parent` | 父任务：`parent` 为对象或 `null`。 |
| `GET` | `/api/jobs/{job_id}/steps` | 仅从 **`ONLINE_PROJECT_STATE_ROOT`** 读取：`runtime/layer_artifacts/{layer_id}/.trajectories/trajectory_*.json`（`agent_steps`）与 `runtime/job_logs/trae_agent_json/{job_id}/step_*/agent_step_full.json`（或 `agent_step.json`）；**不**扫层工作区目录。无数据时 `steps` 为空并附 `note`。 |

### `POST /api/jobs` 行为说明

- **前置**：`command_kind=trae` 时需存在有效 `service_config.yaml`；且全局已有至少一层含 `.git`。
- **必须且仅能**设置 `parent_job_id` 或 `repo_layer_id` 之一（二者都空或都非空会 400）。**不支持**在二者皆无时自动新建空根层再执行。
- **`parent_job_id`**：从父任务对应层**叠新层**（`createStackedLayer`）；叠层前会按与 Web 串行列表相同的 **created_at 序** 删除**该锚点层之后的全部可写层**（含其任务与目录），再 **purge** 该父层在磁盘上的直接子层。
- **`repo_layer_id`**：从指定层叠新层；同样先删串行序中该层**之后**的层，再清理该层的直接子层。
- **`git_branch`**：请求体可携带，**当前未在启动前执行 `git checkout`**（字段预留）。
- **`command_kind=trae`**：优先 `TRAE_CLI`，否则 `{TRAE_VENV}/bin/trae-cli`，否则 venv 内 `python -m trae_agent.cli run …`；若均不可用，则 **stub**：`bash -lc` 输出占位说明并以 0 退出。
- **`command_kind=shell`**：在层工作目录 `bash -lc` 执行全文。

### 运行中控制与清理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/jobs/{job_id}/interrupt` | 向子进程发送 `SIGTERM`；运行中记为 `interrupted`。 |
| `POST` | `/api/jobs/{job_id}/redo` | **501**，正文含 `detail` 说明（未实现删层重建并重跑）。 |
| `POST` | `/api/jobs/{job_id}/continue` | **501**（未实现中断后继续）。 |
| `DELETE` | `/api/jobs/{job_id}` | 删除任务记录并删除该任务对应可写层目录（实现见 `deleteJob`）。 |
| `POST` | `/api/jobs/reset` | 清空任务、删除已知层目录；**未删除** `commands.json`、`job_events/`、`materialized*` 等；响应含 `jobs_cleared`、`layers_removed`。 |

任务状态取值：`pending`、`running`、`completed`、`failed`、`interrupted`。记录中含 `layer_id`、`layer_path`、`command`、`parent_job_id`、`repo_layer_id`、`output`、`exit_code`、`created_at` 等。

## 可写层与文件

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/layers` | 列出可写层（**不含** `meta_kind=empty` 锚点）。 |
| `GET` | `/api/layers/empty-root` | 返回空层锚点 `layer_id`。 |
| `GET` | `/api/layers/{layer_id}/files` | 层内文件扁平列表（实现上有条数上限，默认与遍历深度以代码为准）。 |
| `GET` | `/api/layers/{layer_id}/files/{file_rel_posix}` | 读取单文件；支持 `max_bytes` 等（见路由实现）。 |
| `GET` | `/api/layers/{layer_id}/children` | 列目录子项；查询参数 `dir` 等。 |
| `GET` | `/api/layers/{layer_id}/diff/parent` | **未提供**该路由（无目录树全文 diff）；变动请用 `diff/parent/files` 与 `diff/parent/file`。 |
| `GET` | `/api/layers/{layer_id}/diff/parent/files` | 相对父层工作目录的条目对比列表（`added`/`removed`/`modified`），见 `layerParentDiff.mjs`；无父层时 `detail` 说明。 |
| `GET` | `/api/layers/{layer_id}/diff/parent/file` | 查询参数 **`path`**：单路径文本 diff（或二进制提示）；无父层 **400**。 |
| `DELETE` | `/api/layers/{layer_id}` | 删除该层及其直接子层（自底向上顺序见 `deleteLayerTree`）。 |
| `POST` | `/api/layers/{layer_id}/queue` | 向指定层添加队列项，返回创建结果。 |

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/repos/clone-log/{layer_id}` | 克隆过程文本日志（内存缓冲）。 |
| `GET` | `/api/repos/bootstrap-clone-log` | 引导批量克隆日志。 |
| `POST` | `/api/project/view` | JSON：`layer_id`。**仅返回 JSON** `status`/`active_tip_layer_id` 占位，**不保证**更新 `onlineProject` 符号链接。 |
| `GET` | `/api/project/active` | 返回 `bootstrap_layer_id` 与 `note`（简化实现）。 |

### 空层锚点（`layer_meta.kind=empty`）

- 服务启动时保证存在 **empty** 锚点目录；**`GET /api/layers` 不列出**这些层。
- 克隆通过 `GET /api/layers/empty-root` + `POST /api/repos/clone` 的 `parent_layer_id` 挂载到锚点。

## 层内 Git

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/layers/{layer_id}/git/branches` | **未实现**（列分支）。 |
| `POST` | `/api/layers/{layer_id}/git/commit` | `git add -A` 与 `git commit -m`。 |
| `POST` | `/api/layers/{layer_id}/git/push` | `git push`；支持 `ephemeral_ssh_private_key`、`target_branch`（与 clone 类似）。 |
| `GET` | `/api/layers/{layer_id}/git/commit/latest-log` | `git log -1 --stat` 文本。 |
| `GET` | `/api/git/identity` | **占位**：固定返回空 `user.name` / `user.email`。 |
| `POST` | `/api/git/identity` | **占位**：返回 `ok`，**未**持久化到 git config。 |

- **任务云执行日志（git 摘要）**：commit/push 前后向任务云 `…/git-clone-progress/` 的上报**当前未实现**（克隆引导进度以 `bootstrap.mjs` 为准）。

## 实时事件（SSE）

- **路径**：`GET /api/events/stream?access_token=<ACCESS_TOKEN>`
- **格式**：`text/event-stream`，每条为 `data: <JSON>\n\n`。连接后首条一般为 `{"type":"connected"}`。
- **保活**：由 `sseHub.mjs` 定期发送注释行（如 `: ping`）。
- **浏览器**：`EventSource` 无法自定义 Header，**必须**用查询参数传令牌。

常见 `type` 包括：`connected`、`job_created`、`job_started`、`job_output`（含 `chunk`）、`job_finished`、`service_ready` 等；具体以 `broadcast(...)` 调用为准。客户端应兼容未知类型。

## Web 控制台

- **入口**：`GET /ui/{access_token}` — 路径令牌须与 `ACCESS_TOKEN` 一致，否则 **401**。本地开发可设 `ACCESS_TOKEN=dev-local-token`，页面为 **`http://localhost:8765/ui/dev-local-token`**。
- **富文本呈现声明（表驱动 + 编辑器契约）**：控制台步骤区等按 JSON **声明各字段如何渲染**（纯文本 / 富文本 iframe 等），数据来自 **`GET /api/ui/agent-render-hints`**（查询参数或 `X-Access-Token` 与受保护 API 一致）。响应内 **`rich_text_editor`** 块提供与 **`sanitizeMachineContainerHtml`（`src/htmlSanitize.mjs`）逐字段一致** 的 **`html_allowlist`**（标签与属性表），供富文本编辑器配置白名单、导出校验或与 `presentation_modes.rich_iframe` 对齐；人读约定仍以业务侧 `machine_container.md` §7 为准。浏览器可在 **`GET /ui/{access_token}/render-hints`** 新窗口查看格式化后的该 JSON（与上述 API 同源）。
- Docker 镜像从构建上下文复制 **`onlineServiceJS/static`**（与本包同源）；若缺少静态文件，返回简易 HTML 提示。
- **页眉**：展示 **`REPO_ROOT`** 宿主仓库未推送提交数依赖 `GET /api/dev/service-repo-git-push`。**当前为占位响应**（不跑 `git rev-list`）。
- **可写层变动**：依赖 `GET /api/layers/{layer_id}/diff/parent/files` 与 `GET /api/layers/{layer_id}/diff/parent/file?path=…`。由 `src/layerParentDiff.mjs` 对父层与当前层工作目录做递归条目对比（大目录有条目上限）；无父层时 JSON 带 `detail` 说明。
- 页面通过全局注入的访问令牌调用 API，并用 **`GET /api/events/stream?access_token=…`** 建立 `EventSource`。

### 该页实际调用的 HTTP 接口（与 `dev-local-token` 控制台一致）

| 方法 | 路径 | 用途摘要 |
|------|------|----------|
| `GET` | `/api/dev/service-repo-git-push` | 页眉：宿主仓库未推送提交数；**占位** |
| `GET` | `/api/ui/agent-render-hints` | 步骤等字段的呈现声明（表驱动）及 **`rich_text_editor`（富文本编辑器 HTML 白名单 JSON，与净化实现同源）**；页眉按钮「富文本呈现声明」新窗口同源展示。 |
| `GET` | `/api/events/stream` | SSE：`?access_token=` 必填。 |
| `POST` | `/api/config` | `multipart/form-data`，字段 `file`，上传 `service_config.yaml`。 |
| `GET` | `/api/config` | 拉取当前 YAML 到编辑区。 |
| `POST` | `/api/project/view` | JSON：`{"layer_id":"…"}`；**不保证写 symlink** |
| `GET` | `/api/requirements/task-gate` | 是否允许新建任务（`clone_done`）。 |
| `GET` | `/api/layers/empty-root` | 克隆前取空层锚点 `layer_id`。 |
| `POST` | `/api/repos/clone` | 克隆到新建可写层 |
| `GET` | `/api/repos/clone-log/{layer_id}` | 克隆日志轮询。 |
| `GET` | `/api/repos/bootstrap-clone-log` | 启动引导批量克隆日志。 |
| `GET` | `/api/jobs` | 任务列表与卡片。 |
| `GET` | `/api/jobs/{job_id}` | 单条任务刷新。 |
| `GET` | `/api/jobs/{job_id}/steps` | 步骤手风琴数据；**常为空** |
| `GET` | `/api/jobs/{job_id}/parent` | 「查询父任务」调试区。 |
| `POST` | `/api/jobs` | 创建任务；**须** `parent_job_id` 或 `repo_layer_id` |
| `POST` | `/api/jobs/{job_id}/interrupt` | 中断。 |
| `POST` | `/api/jobs/{job_id}/redo` | 重新执行；**501** |
| `POST` | `/api/jobs/{job_id}/continue` | 继续；**501** |
| `DELETE` | `/api/jobs/{job_id}` | 删除任务。 |
| `POST` | `/api/jobs/reset` | 「重置」；**清理范围见上文** |
| `GET` | `/api/layers` | 下拉与层图（不含 `empty` 锚点）。 |
| `DELETE` | `/api/layers/{layer_id}` | zTree「删除该层」。 |
| `POST` | `/api/layers/{layer_id}/queue` | 运行中「加入队列」；**未实现** |
| `GET` | `/api/layers/{layer_id}/diff/parent/files` | 「可写层变动浏览」变动路径列表。 |
| `GET` | `/api/layers/{layer_id}/diff/parent/file` | 查询参数 **`path`**；选中路径的 diff 正文。 |
| `GET` | `/api/layers/{layer_id}/git/commit/latest-log` | 「最后一次提交」。 |
| `POST` | `/api/layers/{layer_id}/git/commit` | JSON：`message`（可选）；「提交」。 |
| `POST` | `/api/layers/{layer_id}/git/push` | 「推送」；可带 `ephemeral_ssh_private_key`。 |
| `GET` | `/api/layers/{layer_id}/children` | 层内文件树分页。 |
| `GET` | `/api/layers/{layer_id}/files/{file_rel_posix}` | 读取选中文件内容。 |

未在 `index.html` 中直连的接口（如 `GET /api/layers/{layer_id}/files` 仅前缀列表、`GET /api/jobs/{job_id}/events`、`GET /api/layers/{layer_id}/git/branches` 等）**部分路由未实现**；本服务无 `/docs`、`/openapi.json`。

## 层命名

`layer_id` 形如 `YYYYMMDD_HHMMSS_xxxxxx`（时间戳 + 6 位十六进制后缀）。

## 本地启动示例

```bash
export ACCESS_TOKEN='your-secret'
export REPO_ROOT="/path/to/trae-agent"   # 可选；默认为 onlineServiceJS 的上一级
cp trae_config.yaml.example onlineProject_state/runtime/service_config.yaml   # 编辑密钥；或用 UI/API 上传

cd onlineServiceJS
npm install
node src/server.mjs
# 或: PORT=8765 node src/server.mjs
```

浏览器：`http://127.0.0.1:8765/ui/your-secret`
本 Skill：`http://127.0.0.1:8765/skill.md`

## Docker 与多架构镜像

构建**上下文必须为 `trae-agent` 仓库根目录**：

```bash
docker build -f onlineServiceJS/Dockerfile -t your-registry/trae-online-js:latest .
docker run --rm -p 8765:8765 -e ACCESS_TOKEN=dev your-registry/trae-online-js:latest
```

构建时可经 `ARG`/`ENV` 传入 `ACCESS_TOKEN`、`TaskApiEndPoint` 等（见 `onlineServiceJS/Dockerfile`）。镜像内已含 `/app/trae_config.yaml.example`，可复制为运行时配置后再改密钥或通过 `/api/config` 上传。

## 注意事项

- 任务输出在内存与 `jobs_state.json`；服务重启后原 `running` 会记为 `interrupted`（见 `loadState`）。
- 子层叠加通过符号链接共享父层 `.git`（`layerFs.mjs`）。
- **`git_destructive_locked`**：未实现防误操作语义，字段恒为 `false`。
- **`POST /api/jobs/reset`** 会删除层目录，慎用；清理范围见上文（不涵盖全部运行时旁路文件）。
- 配置、克隆日志与任务输出可能含敏感信息，请妥善保管 `ACCESS_TOKEN` 与运行时文件。

## 功能与限制（速查）

| 项目 | 说明 |
|------|------|
| OpenAPI `/docs` | 无 |
| `POST /api/jobs` 空根层 | 不支持；须 `parent_job_id` 或 `repo_layer_id` |
| `redo` / `continue` | **501** |
| `ssh_identity_file` / `branch` / `depth`（clone） | 已实现 |
| `GET .../diff/parent` 全文 | 未提供；`diff/parent/files` 与 `diff/parent/file` 已实现（`layerParentDiff.mjs`） |
| `POST .../queue` | 已实现 |
| `GET .../git/branches`、`GET .../jobs/.../events` | 未实现 |
| `project/view`、`project/active`、`dev/service-repo-git-push`、`git/identity` | 占位或简化 |
| `command_kind=trae` 无 venv | stub 成功退出 |
| 就绪日志 | `[onlineServiceJS] server listening ...` |

静态控制台资源位于 **`onlineServiceJS/static`**，构建镜像时由 Dockerfile 复制进镜像。
