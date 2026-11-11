# Agent 服务

独立 Python 3.12 工程。所有命令在 `agent/` 下运行：

```bash
uv sync --frozen --group dev
cp .env.example .env
# 编辑 .env 的 API Keys、模型 Key、Sandbox URL 和共享 Token。
uv run uvicorn cloudagent.api:app --host 127.0.0.1 --port 8000 --workers 1
uv run pytest -q
uv run ruff check .
```

必须先启动 Sandbox Manager。Agent 只通过 HTTP 调用 Manager，不依赖其 Python 包，不需要 Docker SDK 或 socket。运行图由官方 deepagents.create_deep_agent 创建，底层是 LangGraph。

## 配置

`CLOUDAGENT_AGENTS_DIR` 默认为 `agents`，相对启动进程的工作目录。启动时扫描其中的 `*.yaml`，严格验证；修改后重启生效。

```yaml
id: assistant
model: gpt-4.1-mini
description: 业务助手
system_prompt_file: assistant.md
# 也可使用 system_prompt: 内联内容，二选一。
temperature: 0
tools: [read, write, edit, ls, grep, todo, load_skill, task]
skill_files:
  research: skills/research/SKILL.md
subagents: [analyst]
max_iterations: 12
```

- 提示词和 skills 文件只能位于配置目录中，路径越界和外部符号链接会被拒绝。
- Agent 名称必须唯一；未知工具、缺失子智能体和子智能体循环在启动时失败。
- `tools` 是执行白名单，不是提示词建议。`task`、`load_skill` 也要显式启用。
- 子智能体的工具权限不超过父智能体，不能通过委派扩大权限；子智能体共享当前工作区，但对话上下文和 todo 独立。
- 官方 SkillsMiddleware 从请求私有的虚拟 /skills/ 目录发现技能元数据；load_skill 按需返回允许的内容，不自动运行技能脚本或访问宿主机文件。
- task 使用官方 SubAgentMiddleware，模型参数为 description/subagent_type；HTTP客户端不直接发起工具调用。
- 官方框架默认的文件工具不会直接开放，平台仅注册允许的RPC工具；未配置的通用子智能体和技能动态工具也不会开放。
- 客户端传入的 system 消息按用户偏好处理，不覆盖运营者配置的顶层系统提示词。
- 只有运营者能修改配置，不提供让终端用户上传或执行任意 Python 插件的接口。

## 鉴权与会话

`CLOUDAGENT_API_KEYS` 是 JSON 对象：`{"random-token-for-a":"tenant-a","random-token-for-b":"tenant-b"}`，每个 Token 至少 24 个字符。映射到同一租户的 Key 共享该租户资源。首版所有已认证租户都可以选择已配置的 Agent。

`X-Session-ID` 允许 1–64 个英文字母、数字、下划线或连字符。服务端以租户与会话共同确定工作区，客户端的 `user` 字段不会改变身份。客户端负责提交完整对话历史。

- 同一租户 + 同一会话同时运行：409。
- 超出全局或租户并发上限：429。
- 上游模型或运行时失败（非流式）：502；总运行超时：504。
- 流式响应在输出开始后出错：200 SSE 中包含 error 帧。

使用 HTTPS 和足够随机的独立密钥。不要将模型 Key 或 Manager Token 发送到调用 Agent 的客户端。
