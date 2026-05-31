# 领域笔记：容器层图与引导状态（轻量）

> 本迭代为 Node 回归防护，非 Python DDD 交付物。记录边界供审查对齐。

## 限界上下文

- **ContainerRuntime**：`bootstrap.mjs` — 引导产生的 `bootstrapCloneLayerId`
- **LayerOrchestration**：`jobsRuntime.mjs` — 层删除、快照、SSE 镜像

## 聚合

- **LayerDeletion**（根：layer_id）：删除树 + 触发 LayerGraphSnapshot 发布
- **LayerGraphSnapshot**（值对象）：`layers`, `jobs`, `bootstrap_layer_id`

## 不变量

- 调用 `mirrorLayerGraphToTaskCloudSSE` 前必须能解析 `bootstrapCloneLayerId`（import 或注入；当前为 import）
