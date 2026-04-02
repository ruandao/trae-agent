# Trae Online Service Skill

面向自动化代理/工具的说明：如何通过 HTTP 调用本仓库中的 **onlineService**，远程推送配置、在分层工作区中执行 `trae-cli run`、查看输出与中断任务，并通过 SSE 订阅事件。

## 环境变量

| 变量 | 说明 |
|------|------|
| `ACCESS_TOKEN` | **必填**。所有受保护接口须携带与此相同的令牌（查询参数或 Header）。 |
| `REPO_ROOT` | 可选。Git 仓库根目录，默认 `onlineService` 的上一级目录。 |
| `TRAE_VENV` | 可选。含 `trae-cli` 的虚拟环境根路径（使用 `TRAE_VENV/bin/activate`），默认 `{REPO_ROOT}/.venv`。 |
| `ONLINE_PROJECT_LAYERS` | 可选。可写层根目录，默认 `{REPO_ROOT}/onlineProject/layers`。 |

配置文件固定路径：`onlineService/runtime/service_config.yaml`（由 API 写入）。

## 公开端点（无需令牌）

- `GET /skill.md` — 本 Skill 文档（Markdown）。
- `GET /docs` — FastAPI 自动文档（若未关闭）。

## 受保护 API

所有以下接口需同时满足其一：

- 查询参数：`?access_token=<ACCESS_TOKEN>`
- 请求头：`X-Access-Token: <ACCESS_TOKEN>`

### 配置

- `POST /api/config` — `multipart/form-data`，字段名 `file`，内容为 YAML。校验通过后写入 `service_config.yaml`。
- `POST /api/config/raw` — 查询参数 `yaml=`（仅适合较短内容；大文件请用 multipart）。
- `GET /api/config` — 返回当前 YAML 文本。

### 任务（指令）

- `POST /api/jobs` — JSON：`{ "command": "自然语言或任务描述", "parent_job_id": null | "<uuid>" }`  
  - 无 `parent_job_id`：在 `onlineProject/layers/<时间戳_随机>/` 新建空可写层并执行。  
  - 有 `parent_job_id`：在上一次任务对应目录内容上**复制**出新层（便携版“叠加可写层”），再执行。  
  - 执行命令等价于：  
    `source <venv>/bin/activate && trae-cli run "<command>" --config-file=<service_config.yaml> --working-dir=<层目录>`  
- `GET /api/jobs` — 列出任务（含状态、`output` 聚合输出、`parent_job_id`、`layer_id`）。  
- `GET /api/jobs/{job_id}` — 单条任务详情。  
- `GET /api/jobs/{job_id}/parent` — 查看**上一层指令任务**（父任务完整记录；根任务返回 `parent: null`）。  
- `POST /api/jobs/{job_id}/interrupt` — 对运行中任务发送 `SIGTERM`（进程组）。

### 实时事件（SSE）

- `GET /api/events/stream?access_token=<ACCESS_TOKEN>`  
  - `text/event-stream`，JSON 行事件，主要 `type`：`connected`、`job_created`、`job_started`、`job_output`、`job_finished`、`job_interrupt_requested`。  
  - 浏览器 `EventSource` 无法自定义 Header，因此**必须**使用查询参数传递令牌。  
  - 约 30s 无事件会发送注释行 `: ping` 作为保活。

## Web 控制台

- `GET /ui/{access_token}` — 路径中的令牌须与 `ACCESS_TOKEN` 一致，否则 401。页面内调用上述 API，并建立 SSE 连接。

## 层命名

`layer_id` 形如 `YYYYMMDD_HHMMSS_xxxxxx`，便于从目录名判断创建时间。

## 本地启动示例

```bash
export ACCESS_TOKEN='your-secret'
cp trae_config.yaml onlineService/runtime/service_config.yaml   # 或使用 UI/API 上传
cd onlineService
# 依赖：在仓库根目录执行 uv pip install -r onlineService/requirements.txt（或可用 pip）
../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

浏览器打开：`http://127.0.0.1:8765/ui/your-secret`

## Docker 与多架构镜像

仓库根目录执行（需已启用 buildx；多架构推送需登录镜像仓库）：

```bash
chmod +x onlineService/docker-build.sh
PUSH_IMAGE=1 IMAGE=your-registry/trae-online:latest onlineService/docker-build.sh
```

构建时通过 `Dockerfile` 的 `ENV ACCESS_TOKEN=` 传入默认令牌；运行时仍可用 `-e ACCESS_TOKEN=...` 覆盖。

## 注意事项

- 任务输出保存在内存与 `onlineService/runtime/jobs_state.json`；服务重启后运行中任务会记为 `interrupted`。  
- 父层叠加采用目录复制，大型工作区可能较慢；在 Linux 上可自行改为 overlay 挂载方案。  
- 配置与日志可能包含密钥，请妥善保管 `ACCESS_TOKEN` 与配置文件。
