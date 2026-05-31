# Domain Model: relayToTrae 启动（轻量）

## Bounded Contexts

- **RelayOrchestration**：go_relay 直启会话
- **ContainerRuntime**：onlineServiceJS token / reachability / post-listen
- **TaskAgentGateway**：taskAgentSupport → Django internal

## Aggregates

- **PostListenWorkspaceBootstrap**（根）
  - 拉取 `BootstrapRepoInputs`
  - 可选 `WorkspaceCloneLayer`
  - **AgentRuntimeConfigArtifact**（`service_config.yaml`）

## Domain Events

- `AgentConfigMaterialized(path)` — 配置写入完成
- `ContainerEndpointRegistered` — reachability 成功（已有）

## Repository / Ports

- `FeatureParamsEnvPort` — POST feature-params-env
- `AgentConfigFilesystemPort` — mkdir + writeFile（由 `materializeAgentConfigFile` 封装）
