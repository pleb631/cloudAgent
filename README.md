# cloudAgent

基于 **FastAPI + 官方 Deep Agents + 官方 OpenSandbox** 的配置驱动业务 Agent 平台。

- Agent 运行图由 `deepagents.create_deep_agent` 创建，实际使用官方子智能体和 Skills 中间件，底层是 LangGraph。
- 沙箱生命周期、文件 API、命令执行、execd 和出站网络策略由 **OpenSandbox 1.2.0** 提供，不再维护自建 Sandbox Manager。
- 本地 YAML 配置系统提示词、模型、工具白名单、skills 和子智能体。
- OpenAI Chat Completions 文本子集，支持普通响应、SSE 流式响应和 `/v1/models`。
- `execute / read / write / edit / ls / grep / todo`、按需加载 skill、子任务委派。
- API Key → 租户映射，独立租户/会话沙箱、并发与会话数量限制、请求和运行超时。

官方来源：[Deep Agents](https://github.com/langchain-ai/deepagents)、[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox)（Alibaba 原仓库现重定向至此）。

## 目录

```text
agent/                       # 业务 API 与官方 Deep Agents 集成
  src/cloudagent/            # API、配置、运行时、官方 OpenSandbox SDK 适配
  agents/                    # YAML、系统提示词、SKILL.md
  tests/                     # 单元、协议和真实 OpenSandbox 集成测试
  pyproject.toml / uv.lock    # 独立依赖
  Dockerfile / .env.example
sandbox/                     # 官方 OpenSandbox 的部署配置，不含自建管理 API
  config.toml                # 官方服务端配置
  Dockerfile                 # 安装锁定的官方 opensandbox-server
  pyproject.toml / uv.lock    # 官方服务端依赖与配置测试
compose.yaml                 # Linux Docker 宿主机部署
```

## 快速启动

需要 Linux Docker 宿主机、Docker Engine 和 Docker Compose v2。当前 Compose 使用宿主机网络，两个受信任服务明确只监听 `127.0.0.1`；执行容器使用独立网络命名空间。

1. 复制根目录 `.env.example` 为 `.env`。
2. 为用户 API Key 和 OpenSandbox 服务端 Key 分别生成随机值，填入 `.env`，并设置模型服务 Key。
3. 启动官方组件：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
docker pull opensandbox/execd:release-1.2.0
docker pull opensandbox/egress:release-1.2.0
docker pull python:3.12-slim
docker compose up --build -d
curl http://127.0.0.1:8000/healthz
```

默认执行镜像只是 `python:3.12-slim`，不额外安装业务库。通过 `CLOUDAGENT_SANDBOX_IMAGE` 指定经过审核的镜像；后续镜像选择独立于平台集成。不要把 Key 放进请求文本、技能或工作区，也不要提交 `.env`。

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_CLOUDAGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: demo-session" \
  -d '{"model":"assistant","messages":[{"role":"user","content":"创建待办清单，然后在工作区写一份项目计划"}],"stream":true}'
```

`model` 是本地 Agent ID，不是上游模型名。上游模型在 `agent/agents/*.yaml` 配置。省略 `X-Session-ID` 会生成新会话名，并在响应头返回；复用它可在同一服务进程和有效期内继续使用工作区。不同租户的同名会话对应不同 OpenSandbox 实例。

**工作区是临时的。** 默认空闲有效期 600 秒，活动时续期。会话过期、Agent 正常退出会删除沙箱；进程重启不恢复绑定，旧实例在服务端持续运行时按 TTL 清理；服务端故障时需检查残留资源。这里没有持久化业务文件系统，重要成果需要另行导出。

## OpenAI Python 客户端

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="YOUR_CLOUDAGENT_API_KEY")
stream = client.chat.completions.create(
    model="assistant",
    messages=[{"role": "user", "content": "列出工作区文件并分析材料"}],
    stream=True,
    stream_options={"include_usage": True},
    extra_headers={"X-Session-ID": "business-analysis"},
)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

兼容的是 Chat Completions **文本子集**。支持 `model/messages/stream/stream_options/user`，拒绝未知字段；不支持客户端自定义 tools、图片、音频、Responses API 或结构化输出。工具调用由服务端完成。SSE 开始后出错会返回 `error` 数据帧，再发送 `[DONE]`，调用方必须检查错误。

## 开发与验证

```bash
make install
make lint
make test
make smoke
```

`make test` 默认运行单元/SDK替身测试，不需要模型 Key 或 Docker；`make smoke` 使用真实 HTTP 和官方 Deep Agents，但模型与 OpenSandbox 实例均为明确的测试替身。

真实 OpenSandbox 集成测试需要独立的测试 Docker 宿主机和已启动的官方服务：

```bash
export RUN_OPENSANDBOX_TESTS=1
export CLOUDAGENT_OPENSANDBOX_DOMAIN=127.0.0.1:8090
export CLOUDAGENT_OPENSANDBOX_API_KEY=YOUR_TEST_SERVER_KEY
uv run --project agent pytest agent/tests/test_opensandbox_integration.py -q
python scripts/smoke_test.py
```

CI 分开执行单元测试、模拟 HTTP 测试，以及真实官方服务/SDK/容器集成。实际测试状态以对应提交的 GitHub Actions 结果为准。不会自动调用付费真实模型。

## 部署边界

- 首版单 worker、单 API 副本，配额和租户会话映射在进程内；没有分布式调度、管理 UI、计费、对话数据库或持久化 checkpoint。
- OpenSandbox 的服务端 Key 不是业务租户授权。租户检查在 Agent 层，终端用户不能提交 sandbox ID。控制面和 execd 端口不能暴露公网。
- 默认配置采用 `dns+nft` 和显式 deny 网络策略；只做 DNS 过滤不足以阻止直接 IP 连接。
- Docker 共享宿主机内核，不是强隔离 VM。当前执行根文件系统可写，默认 Python 镜像可能以 root 运行。网络 sidecar 是受信任组件，拥有 NET_ADMIN；不能把执行容器的权限/资源限制套用到所有组件上。
- 文件路径只做字面路径限制，官方文件 API 不提供这里所需的无竞争符号链接约束。**边界是各自的容器，不是 `/workspace` 子目录**。没有磁盘配额。SDK HTTP 响应在解析前限制为2MiB，过大目录会报错而不是无限接收；当前不支持目录分页。

详见 [Agent 配置](agent/README.md)、[官方沙箱部署](sandbox/README.md)、[架构](docs/architecture.md)和[安全说明](docs/security.md)。
