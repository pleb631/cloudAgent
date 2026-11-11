# 架构与集成契约

```text
OpenAI-compatible client
  │ Bearer API Key + X-Session-ID
  ▼
Agent service (FastAPI, one process)
  ├─ API Key → tenant
  ├─ YAML registry → system prompt / tools / skills / subagents
  ├─ per-tenant admission + per-session exclusion
  └─ LangGraph model ↔ tools loop
       ├─ OpenAI-compatible upstream model
       ├─ todo / load_skill / task: in-process, run-scoped
       └─ execute / file tools: internal HTTP RPC
            ▼
Sandbox Manager (separate service, one process)
  ├─ dedicated internal bearer token
  ├─ validated operation schema, tenant/session workspace identity
  └─ Docker daemon
       └─ constrained sibling execution container + session volume
```

## 服务间 RPC

`POST /v1/call`，请求头 `Authorization: Bearer <manager-token>`：

```json
{
  "tenant_id": "tenant-a",
  "session_id": "demo",
  "operation": "write",
  "arguments": {"path": "report.md", "content": "hello"}
}
```

操作参数：

| 操作 | 参数 | 行为 |
| --- | --- | --- |
| execute | command, timeout（可选） | 在工作区执行 shell 命令，限制时长与输出 |
| read | path | 读取有大小限制的 UTF-8 文件 |
| write | path, content | 写入 UTF-8 内容，创建所需父目录 |
| edit | path, old, new | 精确替换一次；找不到或有歧义时拒绝 |
| ls | path（默认 `.`） | 有数量上限的目录列表 |
| grep | pattern, path（默认 `.`） | 有上限的字面字符串搜索，不执行用户正则 |

Manager 不接受任意镜像、挂载点、容器启动参数或宿主机路径。业务请求不能绕过工具白名单，因为 RPC 客户端只被运行时已允许的工具调用。

## 配置和运行状态

配置在启动时加载，不支持边运行边热更新。每次请求创建自己的模型消息、graph 状态、todo 和子任务上下文。会话工作区由 Manager 管理，聊天历史由客户端保存。没有 Redis/Postgres 或分布式锁，也没有无限期运行的后台 Agent。

## 扩展方向

1. 引入 Postgres/Redis 做任务状态、会话锁和全局配额。
2. API 身份接入业务 OIDC，并增加租户对 Agent 和工具的细粒度授权。
3. 高风险工具加入人工审批、审计与可恢复任务。
4. 不可信多租户执行迁移到专用节点或 microVM，并加入磁盘、网络与费用配额。
5. 增加持久化 checkpoint、重试策略、可观测性和工作区生命周期管理。

这些是后续方向，不是当前版本已有功能。
