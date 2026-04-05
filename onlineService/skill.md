# Trae Online Service Skill

面向自动化代理与工具的说明：如何通过 HTTP 调用本仓库中的 **onlineService**，推送配置、克隆远程仓库到可写层、在分层工作区中执行 `trae-cli run`、浏览层内文件与轨迹、管理任务生命周期，并通过 SSE 订阅事件。

## 环境变量

| 变量 | 说明 |
|------|------|
| `ACCESS_TOKEN` | **必填**。所有受保护接口须携带与此相同的令牌（查询参数或 Header）。未配置时 SSE 会返回 503。 |
| `REPO_ROOT` | 可选。本仓库根目录，默认 `onlineService` 的上一级目录。 |
| `TRAE_VENV` | 可选。含 `trae-cli` 的虚拟环境根路径（使用 `TRAE_VENV/bin/activate`），默认 `{REPO_ROOT}/.venv`。 |
| `ONLINE_PROJECT_LAYERS` | 可选。可写层根目录，默认 `{REPO_ROOT}/onlineProject/layers`。 |

运行时与任务行为还可通过 `TRAE_JOB_COLUMNS`、`TRAE_JOB_STDOUT_CHUNK_BYTES`、`TRAE_JOB_STEPS_MAX_CELL_CHARS` 等调节（见实现代码）；克隆相关有 `GIT_CLONE_TIMEOUT_SEC`、`GIT_CLONE_MAX_RETRIES` 等。

配置文件固定路径：`onlineService/runtime/service_config.yaml`（由 API 写入）。任务状态持久化：`onlineService/runtime/jobs_state.json`。

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
| `POST` | `/api/jobs/reset` | 中断并清空所有任务，删除 `jobs_state` 中记录，并清理可写层目录（仅匹配层命名规则的子目录）、删除 `commands.json`。返回统计如 `jobs_cleared`、`layers_removed` 等。 |

任务状态取值：`pending`、`running`、`completed`、`failed`、`interrupted`。任务记录中含 `layer_id`、`layer_path`、`command`、`parent_job_id`、`repo_layer_id`、`git_branch`、`output`、`exit_code`、`created_at` 等字段。

## 可写层与文件

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/layers` | 列出可写层；每项可含 `command`（若当前任务列表中有对应层）、`parent_layer_id`、`git_worktree_dirty` 等。 |
| `GET` | `/api/layers/{layer_id}/files` | 查询参数：`prefix`（可选路径前缀）、`max_files`（默认 2000，上限 5000）。 |
| `GET` | `/api/layers/{layer_id}/files/{file_rel_posix}` | 读取单文件；可选 `max_bytes`、`max_text_chars` 限制返回大小。路径为层内相对 POSIX 路径。 |
| `GET` | `/api/layers/{layer_id}/children` | 列目录子项；查询参数：`dir`（相对路径，默认根）、`prefix`、`offset`、`limit`。 |

## 层内 Git（不 push）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/layers/{layer_id}/git/branches` | 列出该层内仓库分支（需存在 `.git`）。 |
| `POST` | `/api/layers/{layer_id}/git/commit` | JSON：`message`（可选，空则服务端可据任务指令与 diff 生成）。对当前工作区 `git add -A` 并 `git commit`（仅本地，不推送）。 |

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

- `GET /ui/{access_token}` — 路径中的令牌须与 `ACCESS_TOKEN` 一致，否则 401。页面内调用上述 API，并建立 SSE 连接。

## 层命名

`layer_id` 形如 `YYYYMMDD_HHMMSS_xxxxxx`（时间戳 + 6 位十六进制后缀），便于从目录名判断创建时间。

## 本地启动示例

```bash
export ACCESS_TOKEN='your-secret'
cp trae_config.yaml onlineService/runtime/service_config.yaml   # 或使用 UI/API 上传
cd onlineService
# 依赖：在仓库根目录执行 uv pip install -r onlineService/requirements.txt（或可用 pip）
../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

浏览器打开：`http://127.0.0.1:8765/ui/your-secret`  
本 Skill 文档：`http://127.0.0.1:8765/skill.md`

## Docker 与多架构镜像

仓库根目录执行（需已启用 buildx；多架构推送需登录镜像仓库）：

```bash
chmod +x onlineService/docker-build.sh
PUSH_IMAGE=1 IMAGE=your-registry/trae-online:latest onlineService/docker-build.sh
```

构建时可通过 `Dockerfile` 的 `ENV ACCESS_TOKEN=` 传入默认令牌；运行时仍可用 `-e ACCESS_TOKEN=...` 覆盖。

## 注意事项

- 任务输出保存在内存与 `jobs_state.json`；服务重启后原 `running` 会记为 `interrupted`。  
- 子层叠加时**不复制** `.git`，而用符号链接指回父层 `.git`，多子层共享同一仓库元数据；与旧版「逐层完整复制」行为不同。  
- `git_destructive_locked` 用于在已有新提交后禁止误删/误中断破坏历史。  
- `POST /api/jobs/reset` 会删除可写层目录下符合命名规则的全部层，慎用。  
- 配置、克隆日志与任务输出可能含敏感信息，请妥善保管 `ACCESS_TOKEN` 与运行时文件。
