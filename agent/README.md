# Agent 服务

独立 Python 3.12 工程：

```bash
uv sync --frozen --group dev
cp .env.example .env
# 编辑用户 API Keys、模型 Key、官方 OpenSandbox 地址和 Key。
uv run uvicorn cloudagent.api:app --host 127.0.0.1 --port 8000 --workers 1
uv run pytest -q
```

先启动官方 OpenSandbox 服务。Agent 使用 `opensandbox==1.2.0` SDK，不需要 Docker SDK 或 Docker socket，也不运行自己的沙箱管理服务。

## Agent 配置

`CLOUDAGENT_AGENTS_DIR` 默认是相对启动目录的 `agents`。启动时加载 `*.yaml`，修改后重启生效。

```yaml
id: assistant
model: gpt-4.1-mini
description: 业务助手
system_prompt_file: assistant.md
# system_prompt 内联内容与 system_prompt_file 二选一。
temperature: 0
tools: [read, write, edit, ls, grep, todo, load_skill, task]
skill_files:
  research: skills/research/SKILL.md
subagents: [analyst]
max_iterations: 12
```

- 提示词和技能文件只能在配置目录内。未知工具、重复 Agent ID、缺失子智能体和循环引用在启动时失败。
- 运行图实际由官方 `deepagents.create_deep_agent` 创建，不再自建 LangGraph 循环。
- 官方 `SubAgentMiddleware` 提供 task，模型参数为 description/subagent_type。子智能体工具/技能权限不超过父级，共享当前沙箱，但上下文和 todo 私有。
- 官方 `SkillsMiddleware` 从本次请求私有的虚拟 `/skills/` 目录发现技能；load_skill 返回允许的内容，不自动执行技能脚本。
- 框架注入的通用文件工具/默认子智能体不会绕过平台白名单；每次模型响应和工具执行都检查权限。
- 文件工具通过官方 SDK 的文件 API 实现，不借 read 工具执行用户 shell。execute 才接受命令。
- 客户端 system 消息视为用户偏好，不能取代运营者的顶层提示词。

## 租户、会话和资源

`CLOUDAGENT_API_KEYS` 为 JSON 映射，Key 至少 24 字符，例如 `{"random-key":"tenant-a"}`。同一租户的多个 Key 共享资源。首版每个已认证租户可以选择所有已配置的 Agent。

`X-Session-ID` 允许 1–64 个英文字母、数字、下划线或连字符。请求的 `user` 字段不改变身份。SDK适配器按租户和会话建立独立 OpenSandbox 对象，不接收终端用户给定的 sandbox ID。

- 同会话并行请求409；超过运行并发429。沙箱会话数受全局/租户配额约束，容量不足作为工具错误返回。
- 每个 sandbox 使用独立随机 execd Token，不向客户端公开。OpenSandbox 服务端 Key 只保存在 Agent 服务配置中。
- 默认执行实例申请 1 CPU、512 MiB、600秒有效期，活动时续期。数量上限默认4，租户上限2；这些是进程内准入限制，不是宿主机可用容量检测，运营者必须按机器预算调低。
- 会话绑定仅在内存，Agent退出会在有限总时限内尝试销毁实例；已知实例清理失败会保留占额并重试，不立即复用槽位；进程崩溃或未知创建结果在官方服务端正常运行时依赖有限TTL清理。不会在重启后恢复工作区。
- 客户端每次提交对话历史，todo和技能状态只存在于当次运行。

执行命令最长30秒，另有API总运行超时。命令取消尝试调用官方 interrupt；SDK失败不会暴露原始URL、Header或Token。文件读写限64KiB，stdout/stderr分别保留最多64KiB，列表和grep限制结果数量。在SDK解析前，每个HTTP响应限制为2MiB，并拒绝压缩响应以防绕过上限；目录过大时直接报错，不提供分页。

`CLOUDAGENT_OPENSANDBOX_DOMAIN` 是 host:port，不含协议；协议由 `CLOUDAGENT_OPENSANDBOX_PROTOCOL` 指定。生产跨机器连接必须使用受信任的TLS/私网方案。

容量估算、sidecar/磁盘限制和资源耗尽处置见[单主机容量与故障处置](../docs/resource-capacity.md)。默认部署不提供覆盖执行容器、sidecar 和磁盘的聚合硬预算。

## 超时与准入配置

根目录 `.env.example` 用于 Compose 的变量展开，`agent/.env.example` 用于直接运行。Compose 显式传递以下可调参数，修改 `.env` 但不重建容器不会更新运行配置：

- `MAX_CONCURRENT_RUNS=20` / `MAX_RUNS_PER_TENANT=4`：请求运行上限（包括不使用沙箱的请求）。
- `MAX_SANDBOX_SESSIONS=4` / `MAX_SANDBOX_SESSIONS_PER_TENANT=2`：沙箱会话上限。
- `MAX_CONCURRENT_MODEL_CALLS=8` / `MAX_MODEL_CALLS_PER_TENANT=4`：实际模型调用并发上限，含子智能体。
- `MAX_MODEL_CALLS_PER_RUN=64` / `MAX_SUBAGENT_INVOCATIONS_PER_RUN=32`：每个根请求与其子任务共享的调用总预算，不是计费金额上限。
- `REQUEST_TIMEOUT=120`：上游模型请求超时；`RUN_TIMEOUT=300`：根请求总运行时限（秒）。
- `MAX_REQUEST_BYTES=1048576`：HTTP 请求体上限（字节）。

以上变量都加 `CLOUDAGENT_` 前缀。沙箱镜像、CPU、内存、TTL 等配置也在示例环境文件中。Compose 内部固定配置目录、loopback OpenSandbox 地址和 HTTP 协议；OpenSandbox Key 由 `OPENSANDBOX_SERVER_API_KEY` 同时提供给两个服务。控制服务内存/CPU限制不覆盖执行容器和 sidecar。启动时校验环境配置，但容量规划和底层资源隔离仍由运营者负责。

模型累计预算覆盖根 Agent、所有后代和自动上下文摘要；生产模型关闭 SDK 自动重试，避免一次预算被隐式放大。达到预算或任一并行工具出现不可恢复异常时，运行会先取消并等待所有已启动工具（包括沙箱命令）收尾，再释放会话。超时包含等待模型并发名额的时间；超时触发取消后，响应结束与名额释放仍等待收尾，可能略晚于时限。失败或取消不会返还已消耗的累计预算。
