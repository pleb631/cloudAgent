# cloudAgent

基于 **FastAPI + 官方 Deep Agents + LangGraph** 的配置驱动业务 Agent 平台首版。Agent 和沙箱是两个独立 Python 工程，可分别安装、测试和部署。

## 已实现

- 本地 YAML 定义 Agent，支持独立系统提示词、模型、工具白名单、skills 和子智能体。
- 使用官方 `deepagents.create_deep_agent` 创建 LangGraph 运行图；使用 OpenAI 兼容模型服务，通过 `langchain-openai` 异步流式调用。
- `execute / read / write / edit / ls / grep / todo`、按需加载 skill、子智能体委派。
- `/v1/chat/completions` 普通响应和 SSE 流式响应，`/v1/models` 列出可选 Agent。
- API Key → 租户映射。会话归属由服务端鉴权决定，不信任请求的 `user` 字段。
- 独立 Sandbox Manager。执行容器是它创建的兄弟容器；Agent 服务不能访问 Docker socket。
- 并发请求、租户限流、同会话互斥、请求大小限制、运行超时与请求取消后的并发名额释放。

本项目实际依赖官方 `deepagents`，使用其 Agent 运行图、SubAgentMiddleware、SkillsMiddleware 和上下文管理能力。文件/执行工具通过平台的受限 RPC 接口接入独立 Sandbox Manager，todo/load_skill 保留平台的命名和权限控制。首版没有管理 UI、持久化对话数据库、计费或生产级调度系统。

## 目录

```text
agent/                      # Agent API / Deep Agents 运行时
  src/cloudagent/           # API、配置、运行时、沙箱 RPC 客户端
  agents/                  # Agent YAML、提示词、skills
  tests/                   # API、配置、运行时测试
  pyproject.toml / uv.lock  # 独立依赖
  Dockerfile / .env.example
sandbox/                    # 独立 Sandbox Manager
  src/sandbox_service/     # RPC、后端、文件操作、容器控制
  tests/                   # 隔离、验证、容器约束测试
  pyproject.toml / uv.lock  # 独立依赖
  Dockerfile               # Manager 镜像
  Dockerfile.runtime       # 实际执行代码的受限镜像
compose.yaml                # 两个服务的集成部署
```

## Docker 快速启动

需要 Python 3.12+（本地开发）、Docker Engine 和 Docker Compose v2。

1. 复制根目录 `.env.example` 为 `.env`。
2. 为 API Key 和 Sandbox Token 分别生成随机值，填入 `.env`，并设置模型服务的 API Key。不要提交 `.env`。
3. 构建执行镜像，再启动服务：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
docker build -t cloudagent-sandbox-runtime:local -f sandbox/Dockerfile.runtime sandbox
docker compose up --build -d
curl http://localhost:8000/healthz
```

默认 API 仅绑定宿主机 `127.0.0.1:8000`，Manager 不发布端口。部署到公网前必须增加 TLS、网关限流和适合业务的身份系统。

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_CLOUDAGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: demo-session" \
  -d '{"model":"assistant","messages":[{"role":"user","content":"创建一个待办清单，然后在工作区写入一份项目计划"}],"stream":true}'
```

`model` 是本地 Agent ID，不是上游模型名。上游模型在 `agent/agents/*.yaml` 中配置。省略 `X-Session-ID` 会创建新工作区标识，返回头包含该标识；继续使用工作区时复用它。不同租户使用相同会话名仍然彼此隔离。

## OpenAI Python 客户端

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="YOUR_CLOUDAGENT_API_KEY")
stream = client.chat.completions.create(
    model="assistant",
    messages=[{"role": "user", "content": "列出工作区文件，分析其中的材料"}],
    stream=True,
    stream_options={"include_usage": True},
    extra_headers={"X-Session-ID": "business-analysis"},
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

兼容的是 Chat Completions 的**文本子集**，不是 OpenAI 全部协议。支持 `model/messages/stream/stream_options/user`，拒绝未知参数；不支持客户端自定义 tools、图片、音频、Responses API 或结构化输出。服务器内部完成工具调用，客户端只接收助手文本与可选累计 usage。SSE 开始后出错会返回 `error` 数据帧再发送 `[DONE]`，调用方需要检查。

## 本地开发与测试

```bash
make install
make test
make lint
make smoke  # 真实 HTTP 端到端：模拟模型 → Deep Agents → 本地 Sandbox Manager
```

每个服务可单独操作：

```bash
cd agent
uv sync --frozen --group dev
uv run pytest -q
# 按 agent/.env.example 设置配置后：
uv run uvicorn cloudagent.api:app --host 127.0.0.1 --port 8000
```

Manager 的独立开发、无 Docker 的只读/文件操作模式及测试见 [sandbox/README.md](sandbox/README.md)。Agent 的配置说明见 [agent/README.md](agent/README.md)。

## 重要边界

- 对话历史由客户端每次传入；首版不存储对话历史，todo 是单次运行状态。
- 并发控制是进程内的，首版每个服务运行 **1 个 worker / 1 个副本**。扩展多副本前需共享锁、配额、会话路由和持久化状态。
- Docker 容器共享宿主机内核，**不等同于安全虚拟机或强多租户边界**。对公网不可信代码应采用独立执行节点、rootless、gVisor/Kata/microVM 等更强边界。
- Manager 拥有 Docker daemon 权限，是高信任基础设施。不要把它的接口、Token 或 Docker socket 暴露给业务用户或 Agent 执行容器。
- 本地开发后端没有 OS 沙箱，禁用 `execute`，不能作为生产隔离措施。
- 客户端断开不会保证立即取消 Manager 中的操作，但执行时长有上限；异常退出的孤儿容器需要运营者检查清理。
- 未设置真实模型密钥时无法执行真实 LLM 请求。自动化测试使用假的模型响应，不消耗模型额度。

详见 [架构与协议](docs/architecture.md) 和 [安全边界](docs/security.md)。
