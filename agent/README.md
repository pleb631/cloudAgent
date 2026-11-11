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
- 默认执行实例申请 1 CPU、512 MiB、600秒有效期，活动时续期。数量上限默认50，租户上限10；运营者应根据机器容量调低。
- 会话绑定仅在内存，Agent退出会尝试销毁实例；进程崩溃或未知创建结果在官方服务端正常运行时依赖有限TTL清理。不会在重启后恢复工作区。
- 客户端每次提交对话历史，todo和技能状态只存在于当次运行。

执行命令最长30秒，另有API总运行超时。命令取消尝试调用官方 interrupt；SDK失败不会暴露原始URL、Header或Token。文件读写限64KiB，stdout/stderr分别保留最多64KiB，列表和grep限制结果数量。在SDK解析前，每个HTTP响应限制为2MiB，并拒绝压缩响应以防绕过上限；目录过大时直接报错，不提供分页。

`CLOUDAGENT_OPENSANDBOX_DOMAIN` 是 host:port，不含协议；协议由 `CLOUDAGENT_OPENSANDBOX_PROTOCOL` 指定。生产跨机器连接必须使用受信任的TLS/私网方案。
