# Trae Online Service Skill

面向自动化代理与工具的说明：如何通过 HTTP 调用本仓库中的 **onlineService**，推送配置、克隆远程仓库到可写层、在分层工作区中执行 `trae-cli run`、浏览层内文件与轨迹、管理任务生命周期，并通过 SSE 订阅事件。

## 环境变量

| 变量 | 说明 |
|------|------|
| `ACCESS_TOKEN` | **必填**。所有受保护接口须携带与此相同的令牌（查询参数或 Header）。未配置时 SSE 会返回 503。 |
| `REPO_ROOT` | 可选。本仓库根目录，默认 `onlineService` 的上一级目录。 |
| `ONLINE_PROJECT_STATE_ROOT` | 可选。运行时状态根目录（其下含 `runtime`、`logs`、`reqLogs`），默认 `{REPO_ROOT}/onlineProject_state`。 |
| `TRAE_VENV` | 可选。含 `trae-cli` 的虚拟环境根路径（使用 `TRAE_VENV/bin/activate`），默认 `{REPO_ROOT}/.venv`。 |
| `ONLINE_PROJECT_LAYERS` | 可选。可写层根目录，默认 `{REPO_ROOT}/onlineProject_state/layers`。 |

运行时与任务行为还可通过 `TRAE_JOB_COLUMNS`、`TRAE_JOB_STDOUT_CHUNK_BYTES`、`TRAE_JOB_STEPS_MAX_CELL_CHARS` 等调节（见实现代码）；克隆相关有 `GIT_CLONE_TIMEOUT_SEC`、`GIT_CLONE_MAX_RETRIES` 等。

配置文件固定路径：`onlineProject_state/runtime/service_config.yaml`（由 API 写入；内容与仓库根目录 `trae_config.yaml.example` 同结构，内含合法内置工具名说明）。任务状态持久化：`onlineProject_state/runtime/jobs_state.json`。Docker 镜像内同一份示例路径：`/app/trae_config.yaml.example`。

## 推荐调用顺序

1. `POST /api/config`（或 `POST /api/config/raw`）推送有效 YAML。
2. 确认虚拟环境存在且含 `trae-cli`（否则创建任务会失败）。
3. `POST /api/repos/clone` 将远程仓库克隆到**新的**可写层（需系统已安装 `git`）。
4. 再 `POST /api/jobs` 创建任务（服务端要求：至少曾成功存在过含 `.git` 的可写层，见下文「任务门控」）。

## 公开端点（无需令牌）

- `GET /skill.md` — 本 Skill 文档（Markdown，`text/markdown; charset=utf-8`）。
- `GET /docs` — FastAPI 自动文档（若未关闭）。
- `GET /openapi.json` — OpenAPI 模式（若未关闭）。

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
| `POST` | `/api/repos/clone` | JSON：`url`（必填），`branch`、`depth`（可选）。在新可写层目录内执行 `git clone`；成功返回 `layer_id`、`layer_path`、`output`。失败时 400，响应体可含 `exit_code`、`output`。克隆过程与结果会通过 SSE 推送（见下文）。 |

## 任务门控

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/requirements/task-gate` | 返回 `clone_done`（布尔）：当前是否在可写层根下**至少存在一个含 `.git` 的层**。创建任务前会再次校验；未满足时会拒绝 `POST /api/jobs`。 |

## 任务（指令）

### 创建与查询

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/jobs` | JSON 正文字段：`command`（必填，非空字符串）、`parent_job_id`（可选）、`repo_layer_id`（可选）、`git_branch`（可选）。 |
| `GET` | `/api/jobs` | 返回 `{ "jobs": [ ... ] }`，每条为任务记录并额外包含 `git_destructive_locked`（布尔）：若任务开始后工作区 `HEAD` 相对记录基线已变化，则禁止中断、重做、继续、删除等破坏性操作。 |
| `GET` | `/api/jobs/{job_id}` | 单条任务详情（同上结构）。 |
| `GET` | `/api/jobs/{job_id}/parent` | 父任务：根任务返回 `parent: null`；若父记录缺失则带 `note`。 |
| `GET` | `/api/jobs/{job_id}/steps` | 从该任务可写层下**最新**的 `.trajectories/trajectory_*.json` 解析 `agent_steps` 等，返回 `trajectory_file`、`task`、`steps`、`note` 等（大字段可能按 `TRAE_JOB_STEPS_MAX_CELL_CHARS` 截断）。 |

### `POST /api/jobs` 行为说明

- **前置**：已存在有效 `service_config.yaml`、venv 可激活，且全局已有至少一层含 `.git`（通常来自克隆）。
- **无 `parent_job_id` 且无 `repo_layer_id`**：在 `onlineProject/layers/<layer_id>/` 新建**空**根层再执行。
- **`parent_job_id` 有值**：从父任务对应目录**复制**出新层（不复制 `.git`，子层通过符号链接共享父层 `.git`），再执行。不可与 `repo_layer_id` 同时出现。
- **`repo_layer_id` 有值**（且无父任务）：从指定 `layer_id` 对应目录复制出新层并链接 `.git`，再执行。
- **`git_branch`**：任务启动前在工作区内执行 `git checkout`；须同时提供 `parent_job_id` 或 `repo_layer_id`（不能单独用于纯空根层）。
- 执行命令等价于：
  `source <venv>/bin/activate && trae-cli run "<command>" --config-file=<service_config.yaml> --working-dir=<层目录>`
- 「继续」类任务在内部使用 `trae-cli run "继续"` 并保留先前输出（见 `POST .../continue`）。

### 运行中控制与清理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/jobs/{job_id}/interrupt` | 对运行中任务向进程组发送 `SIGTERM`。若 `git_destructive_locked` 为真则 400。 |
| `POST` | `/api/jobs/{job_id}/redo` | 删除该任务当前可写层目录，按创建时的来源重新建层并**重新执行同一指令**。运行中/待开始会先尝试中断或取消 runner。 |
| `POST` | `/api/jobs/{job_id}/continue` | 仅当状态为 `interrupted`：保留可写层，以「继续」再次运行 `trae-cli`。 |
| `DELETE` | `/api/jobs/{job_id}` | 从任务列表移除并删除该任务可写层目录；若存在子任务则 400。若 `git_destructive_locked` 为真则 400。 |
| `POST` | `/api/jobs/reset` | 中断并清空所有任务，删除 `jobs_state` 中记录，并清理可写层目录（仅匹配层命名规则的子目录）、删除 `commands.json` 与 `job_events/`；顺带删除 `runtime/job_logs`、`runtime/materialized*` 等可再生缓存。返回如 `jobs_cleared`、`layers_removed`、`runtime_ephemeral_removed`（目录名列表）等。 |

任务状态取值：`pending`、`running`、`completed`、`failed`、`interrupted`。任务记录中含 `layer_id`、`layer_path`、`command`、`parent_job_id`、`repo_layer_id`、`git_branch`、`output`、`exit_code`、`created_at` 等字段。

## 可写层与文件

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/layers` | 列出可写层；每项可含 `command`（若当前任务列表中有对应层）、`parent_layer_id`、`git_worktree_dirty` 等。 |
| `GET` | `/api/layers/{layer_id}/files` | 查询参数：`prefix`（可选路径前缀）、`max_files`（默认 2000，上限 5000）。 |
| `GET` | `/api/layers/{layer_id}/files/{file_rel_posix}` | 读取单文件；可选 `max_bytes`、`max_text_chars` 限制返回大小。路径为层内相对 POSIX 路径。 |
| `GET` | `/api/layers/{layer_id}/children` | 列目录子项；查询参数：`dir`（相对路径，默认根）、`prefix`、`offset`、`limit`。 |
| `GET` | `/api/layers/{layer_id}/diff/parent` | **父层与当前层目录树全文 diff**（`diff -ruN -x .git`）。父层 ID 由 `.git` 符号链接或任务记录解析，与 `GET /api/layers` 返回的 `parent_layer_id` 一致；若无父层则响应体含 `detail` 说明，`diff` 为空。返回 `same`、`diff`、`truncated` 等。 |
| `GET` | `/api/layers/{layer_id}/diff/parent/files` | **相对父层的变动路径列表**。由 `diff -rq -x .git` 解析为 JSON：`changes` 为 `{ path, kind }` 数组，`kind` 为 `modified` \| `added` \| `removed`。无父层时同上，返回 `changes: []` 与 `detail`。条数过多时 `truncated: true`。 |
| `GET` | `/api/layers/{layer_id}/diff/parent/file` | **单路径相对父层的 unified diff**。查询参数 **`path`**（必填）：层内相对 POSIX 路径，规则同读取单文件接口。文件使用 `diff -uN`；目录使用 `diff -ruN -x .git`。无父层时 **400**。响应含 `path_kind`（`file` \| `dir`）、`diff`、`truncated` 等。 |
| `DELETE` | `/api/layers/{layer_id}` | 删除该可写层（含子层时自底向上）；运行中任务会先中断。 |
| `POST` | `/api/layers/{layer_id}/queue` | JSON：`command`、`command_kind`（`trae` \| `shell`），向该层待执行队列追加一条。 |
| `GET` | `/api/repos/clone-log/{layer_id}` | 返回克隆过程文本日志（内存缓冲，克隆结束后可能清空）。 |
| `GET` | `/api/repos/bootstrap-clone-log` | 容器引导阶段批量克隆的日志；无进行中的引导时 `layer_id` 可能为 `null`。 |
| `POST` | `/api/project/view` | JSON：`layer_id`。将仓库根下 `onlineProject` 符号链接指向该层 tip（物化或层目录）。 |
| `GET` | `/api/project/active` | 解析当前 `onlineProject` 指向与 `active_tip_layer_id` 等。 |

## 层内 Git

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/layers/{layer_id}/git/branches` | 列出该层内仓库分支（需存在 `.git`）。 |
| `POST` | `/api/layers/{layer_id}/git/commit` | JSON：`message`（可选，空则服务端可据任务指令与 diff 生成）。对当前工作区 `git add -A` 并 `git commit`（仅本地，不 push）。 |
| `POST` | `/api/layers/{layer_id}/git/push` | 将当前分支 `git push` 到已配置上游；可选 JSON：`target_branch`、`github_auth`。 |
| `GET` | `/api/layers/{layer_id}/git/commit/latest-log` | 该层各 git 根目录最近一次 `git log -1 --stat` 聚合文本。 |
| `GET` | `/api/git/identity` | 当前进程内用于 `git commit` 的 `user.name` / `user.email` 运行时配置（可能为空）。 |
| `POST` | `/api/git/identity` | JSON：`name`、`email`，设置上述运行时身份。 |

## 实时事件（SSE）

- **路径**：`GET /api/events/stream?access_token=<ACCESS_TOKEN>`
- **格式**：`text/event-stream`，每条为 `data: <JSON>\n\n`。连接后首条一般为 `{"type":"connected"}`。
- **保活**：约 30s 无事件发送注释行 `: ping`。
- **浏览器**：`EventSource` 无法自定义 Header，**必须**用查询参数传令牌。

常见 `type` 包括：

- `connected` — 连接建立。
- `repo_clone_finished` — 克隆结束（含 `layer_id`、`status`、`exit_code` 等）。
- `repo_cloned` — 克隆成功后的补充事件。
- `job_created`、`job_started`、`job_output`（含 `chunk`）、`job_finished` — 任务生命周期。
- `job_interrupt_requested`、`job_redone`、`job_continued`、`job_deleted`。
- `jobs_reset` — 全局重置完成。

## Web 控制台

- **入口**：`GET /ui/{access_token}` — 路径中的令牌须与环境变量 `ACCESS_TOKEN` 一致，否则 **401**。本地执行 `onlineService/run_local.sh` 时默认 `ACCESS_TOKEN=dev-local-token`，故开发页为 **`http://localhost:8765/ui/dev-local-token`**（或 `127.0.0.1`）。
- 页面为单页 `static/index.html`：通过全局 `ACCESS_TOKEN` 调用下方接口，并用 **`GET /api/events/stream?access_token=…`** 建立 `EventSource`（浏览器无法为 SSE 自定义 Header，令牌只能走查询参数）。
- **可写层变动**：`GET /api/layers/{layer_id}/diff/parent/files` 列出相对父层路径，再 `GET /api/layers/{layer_id}/diff/parent/file?path=…` 查看单路径 unified diff。

### 该页实际调用的 HTTP 接口（与 `dev-local-token` 控制台一致）

| 方法 | 路径 | 用途摘要 |
|------|------|----------|
| `GET` | `/api/events/stream` | SSE：`?access_token=` 必填。 |
| `POST` | `/api/config` | `multipart/form-data`，字段 `file`，上传 `service_config.yaml`。 |
| `GET` | `/api/config` | 拉取当前 YAML 到编辑区。 |
| `POST` | `/api/project/view` | JSON：`{"layer_id":"…"}`，切换「层 / 任务」选择时更新 `onlineProject` 指向。 |
| `GET` | `/api/requirements/task-gate` | 是否允许新建任务（`clone_done`）。 |
| `POST` | `/api/repos/clone` | 克隆到新建可写层。 |
| `GET` | `/api/repos/clone-log/{layer_id}` | 克隆日志轮询。 |
| `GET` | `/api/repos/bootstrap-clone-log` | 启动引导批量克隆日志。 |
| `GET` | `/api/jobs` | 任务列表与卡片。 |
| `GET` | `/api/jobs/{job_id}` | 单条任务刷新。 |
| `GET` | `/api/jobs/{job_id}/steps` | 步骤手风琴数据。 |
| `GET` | `/api/jobs/{job_id}/parent` | 「查询父任务」调试区。 |
| `POST` | `/api/jobs` | 创建任务（含 zTree 操作栏「创建并执行」）。 |
| `POST` | `/api/jobs/{job_id}/interrupt` | 中断。 |
| `POST` | `/api/jobs/{job_id}/redo` | 重新执行。 |
| `POST` | `/api/jobs/{job_id}/continue` | 继续（仅 `interrupted`）。 |
| `DELETE` | `/api/jobs/{job_id}` | 删除任务。 |
| `POST` | `/api/jobs/reset` | 「重置（中断并清空）」。 |
| `GET` | `/api/layers` | 下拉与 zTree 层图；含 `git_worktree_dirty`、`git_remote` 等。 |
| `DELETE` | `/api/layers/{layer_id}` | zTree 操作栏「删除该层」。 |
| `POST` | `/api/layers/{layer_id}/queue` | 运行中任务时「加入队列」。 |
| `GET` | `/api/layers/{layer_id}/diff/parent/files` | 可写层变动列表 / 心智图父层差异。 |
| `GET` | `/api/layers/{layer_id}/diff/parent/file` | 查询参数 **`path`**（必填）：单路径 diff。 |
| `GET` | `/api/layers/{layer_id}/git/commit/latest-log` | 提交日志面板「最后一次提交」。 |
| `POST` | `/api/layers/{layer_id}/git/commit` | JSON：`message`（可选）；「提交」按钮。 |
| `POST` | `/api/layers/{layer_id}/git/push` | 「推送」按钮（无 body 亦可）。 |
| `GET` | `/api/layers/{layer_id}/children` | 查询参数：`dir`、`prefix`、`offset`、`limit`；层内文件树分页。 |
| `GET` | `/api/layers/{layer_id}/files/{file_rel_posix}` | 查询参数：`max_bytes`、`max_text_chars`；读取选中文件内容。 |

未在 `index.html` 中直连的接口（如 `GET /api/layers/{layer_id}/files` 仅前缀列表、`GET /api/jobs/{job_id}/events`、`GET /api/layers/{layer_id}/git/branches` 等）仍可在 **OpenAPI**（`/docs`、`/openapi.json`）中查阅，供脚本或其它客户端使用。

## 层命名

`layer_id` 形如 `YYYYMMDD_HHMMSS_xxxxxx`（时间戳 + 6 位十六进制后缀），便于从目录名判断创建时间。

## 本地启动示例

```bash
export ACCESS_TOKEN='your-secret'
cp trae_config.yaml.example onlineProject_state/runtime/service_config.yaml   # 编辑密钥；或用 UI/API 上传
cd onlineService
# 依赖：在仓库根目录执行 uv pip install -r onlineService/requirements.txt（或可用 pip）
../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

浏览器打开：`http://127.0.0.1:8765/ui/your-secret`（默认本地令牌时：`http://localhost:8765/ui/dev-local-token`）
本 Skill 文档：`http://127.0.0.1:8765/skill.md`

## Docker 与多架构镜像

仓库根目录执行（需已启用 buildx；多架构推送需登录镜像仓库）：

```bash
chmod +x onlineService/docker-build.sh
PUSH_IMAGE=1 IMAGE=your-registry/trae-online:latest onlineService/docker-build.sh
```

构建时可通过 `Dockerfile` 的 `ENV ACCESS_TOKEN=` 传入默认令牌；运行时仍可用 `-e ACCESS_TOKEN=...` 覆盖。镜像内已含 `/app/trae_config.yaml.example`，可复制为运行时配置：例如 `docker exec ... cp /app/trae_config.yaml.example /app/onlineProject_state/runtime/service_config.yaml` 后再改密钥或通过 `/api/config` 上传。

## 注意事项

- 任务输出保存在内存与 `jobs_state.json`；服务重启后原 `running` 会记为 `interrupted`。
- 子层叠加时**不复制** `.git`，而用符号链接指回父层 `.git`，多子层共享同一仓库元数据；与旧版「逐层完整复制」行为不同。
- `git_destructive_locked` 用于在已有新提交后禁止误删/误中断破坏历史。
- `POST /api/jobs/reset` 会删除可写层目录下符合命名规则的全部层，慎用。
- 配置、克隆日志与任务输出可能含敏感信息，请妥善保管 `ACCESS_TOKEN` 与运行时文件。
