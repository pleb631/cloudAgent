# cloudAgent：OpenHands 独立方案

基于官方 **OpenHands Software Agent SDK / Tools / agent-server 1.62.0** 的配置驱动业务 Agent API。这是本分支唯一实现，根目录默认部署直接启动 OpenHands；旧方案保留在主分支。

核心区别：用户文件已经在服务器上；服务端将预配置的租户目录直接读写挂载到执行容器 `/workspace`。没有上传接口，也不复制整个工作区。Agent 的编辑立即反映在原目录。

**仅供受信任用户使用。** Docker 共享内核，模型凭据进入执行容器，没有硬磁盘配额或出站网络拒绝策略。上线前请读下文安全边界。

## 目录

```text
openhands/
  src/cloudagent_oh/     # 业务 API、配置、官方 SDK 集成与容器生命周期
  agents/               # 受信任 YAML、系统提示词、SKILL.md
  tests/                # 本方案测试
  pyproject.toml        # 独立 Python 依赖
  Dockerfile
  .env.example
compose.yaml            # 仓库根目录默认部署
```

## 本机启动（Linux Docker 宿主机）

需要 Python 3.12+、`uv`、Docker Engine，以及可访问模型服务和拉取镜像的网络。先准备一个专用的测试工作区，不要直接试跑唯一一份生产数据。

```bash
# 在仓库根目录；此目录是实际 Docker 宿主机目录。
mkdir -p "$HOME/cloudagent-demo"
export CLOUDAGENT_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export CLOUDAGENT_OH_API_KEYS="{\"$CLOUDAGENT_API_KEY\":\"demo\"}"
export CLOUDAGENT_OH_WORKSPACE_ROOTS="{\"demo\":\"$HOME/cloudagent-demo\"}"
# 在当前 shell 安全地提供模型 Key，不写入仓库。
read -rsp 'Model API key: ' CLOUDAGENT_OH_LLM_API_KEY; echo
export CLOUDAGENT_OH_LLM_API_KEY
export CLOUDAGENT_OH_AGENTS_DIR="$PWD/openhands/agents"
docker build -t cloudagent-openhands-server:1.62.0 -f openhands/server/Dockerfile openhands
cd openhands
uv sync
uv run uvicorn cloudagent_oh.api:app --host 127.0.0.1 --port 8001 --workers 1
```

另开终端时需要重新提供相同的 `CLOUDAGENT_API_KEY`；不要泄露或提交该值。可在同一已导出变量的 shell 后台启动服务再测试：

```bash
curl http://127.0.0.1:8001/healthz
curl http://127.0.0.1:8001/v1/models -H "Authorization: Bearer $CLOUDAGENT_API_KEY"
```

默认模型服务地址为 `https://api.openai.com/v1`；其他兼容服务可设置 `CLOUDAGENT_OH_LLM_BASE_URL`，并在 Agent YAML 中选择供应商支持的模型名。默认每容器 1 CPU、1 GiB 内存、256 PID；请求运行上限 300 秒，全局最多 4 个活动请求。可通过 `CLOUDAGENT_OH_SANDBOX_CPUS`、`CLOUDAGENT_OH_SANDBOX_MEMORY`、`CLOUDAGENT_OH_SANDBOX_PIDS`、`CLOUDAGENT_OH_RUN_TIMEOUT`、`CLOUDAGENT_OH_MAX_CONCURRENT_RUNS` 调整。

## Docker Compose 启动

```bash
cp .env.example .env
# 编辑 .env：API Key、专用模型 Key、Docker 宿主机绝对目录映射。
# 预先创建各租户目录，并让执行镜像的非 root 用户可读写；不要 chmod 整个服务器目录。
docker compose --profile image build
docker compose up -d openhands-api
```

这是 Linux host-network 部署，监听 `127.0.0.1:8001`。API 容器以只读方式看见相同宿主路径来验证配置；每个执行容器只得到一个租户根目录的读写挂载。主机模型地址不能写成仅容器自身可见的 `127.0.0.1`，要使用从执行容器可达的模型服务地址。镜像用户 UID/GID 与目录权限须在目标服务器核对。

服务端镜像基于官方 `1.62.0-python`，附加一个锁定版本的配置策略入口，禁用 SDK 默认的工作区/用户插件和文件子 Agent 自动发现。这些路径不受 `load_project_skills=False` 控制；不禁用就可能扩张 YAML 工具配置。显式传入的原生 Agent、技能、子 Agent 仍然运行。升级 SDK 必须更新策略合同测试，不可只换镜像 tag。

## 接口

支持 OpenAI Chat Completions 的文本子集：

- `GET /healthz`：进程健康检查，不代表模型与 Docker 已联通。
- `GET /v1/models`：已配置的 Agent ID，需要 Bearer Key。
- `POST /v1/chat/completions`：普通 JSON 或 SSE；`model` 为本地 Agent ID，不是上游模型名。
- `messages` 支持 `system`、`user`、`assistant` 文本；不支持图片、客户端工具调用或完整 OpenAI 参数集。未知字段会被拒绝。

```bash
curl -N http://127.0.0.1:8001/v1/chat/completions \
  -H "Authorization: Bearer $CLOUDAGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'X-Session-ID: demo' \
  -d '{"model":"assistant","messages":[{"role":"user","content":"检查 /workspace 中的项目，在 /workspace/NOTES.md 写下改进建议，不修改其他文件"}],"stream":true,"stream_options":{"include_usage":true}}'
```

将 `stream` 改为 `false` 可获取完整 JSON。SSE 内容来自 SDK token callback 实际增量，不将完整答案重新切块；开始返回 assistant role，正常结束返回 finish_reason、可选 usage 和 `[DONE]`。流开始后的失败通过 SSE error 对象返回，不能再改变 HTTP 状态码。

### 文件持久，对话不持久

每个请求都创建新 agent-server 容器和 SDK Conversation，结束后销毁容器。只有 bind mount 中的文件保留。`messages` 作为本次请求的文本历史输入，客户端需要自行保留并重新提交所需历史。

`X-Session-ID` 用于关联请求，并在响应头返回；复用它**不会**恢复 SDK 对话。不同 session ID 也不会分配独立目录：同一租户始终使用其服务器配置的工作区。客户端 `user` 字段不参与租户鉴权。

同一租户同时只允许一个活动请求，冲突返回 `409`；全局并发限额返回 `429`。断开连接、取消、超时会走容器清理，但已经写入的文件不会自动回滚。

## Agent 配置

YAML 由运维维护，修改后重启服务。示例：

```yaml
id: assistant
description: 服务器项目助手
model: openai/gpt-4.1-mini
temperature: 0
system_prompt_file: assistant.md
tools: [terminal, file_editor, glob, grep, task_tracker, task_tool_set]
skill_files:
  careful_editing: skills/editing.md
subagents: [analyst]
max_iterations: 12
```

提示词可改用内联 `system_prompt`，不能与 `system_prompt_file` 同时填写。提示词和 skill 路径必须位于 `agents_dir` 内。技能作为受信任本地文本进入原生 `AgentContext`，不是从用户项目自动运行插件；不运行时加载公共技能或插件。

工具使用官方实现：`terminal`、`file_editor`、`glob`、`grep`、`task_tracker`、`task_tool_set`。子智能体使用原生委派能力，必须是无进一步委派能力的叶子 Agent；工具权限不能超出父 Agent（配置允许额外的 `task_tracker`）。工具白名单限制 Agent 可调用的工具，不是 shell 命令白名单，也不是工作目录强制隔离。

## 安全与运维边界

1. **备份第一。** 原目录可直接被改写或删除。先使用 Git、快照或备份；容器销毁、请求取消和超时都不恢复已改动文件。
2. **路径由服务器控制。** 每个租户目录必须是已有绝对路径，不能含符号链接；租户根目录不能相同或互相嵌套。只挂载该租户所需的最小目录。
3. **Docker daemon 解析源路径。** 配置的是 Docker 宿主机实际路径，不是 API 容器独有路径。若 API 自身在 Docker 中运行，它还需看到相同路径以做配置校验。不能只在 API 容器中创建目录就认为宿主机已有该目录。
4. **不要暴露控制面。** 管理服务持有 Docker socket，相当于高权限宿主机控制能力；不要将 socket 挂到 agent-server。API 应放在受控网络或具备 TLS 的鉴权代理后。
5. **资源限制不是完整隔离。** 容器 CPU、内存、PID 限制和请求超时无法防止磁盘写满、外传或内核漏洞利用。没有硬磁盘配额、无出站访问拒绝策略，不适合敌对多租户。
6. **模型 Key 在执行环境中。** 使用专用、低额度、可撤销的模型 Key；不要把生产凭据放进工作区、提示词或技能。
7. **不要多 worker。** 首版请求准入是进程内控制，按单实例、单 worker 部署；不同部署不得指向同一租户目录，否则仍可能并行写入。

该方案使用官方 SDK，不包含完整 OpenHands 产品的 UI、账户、计费与持久会话管理。SDK 1.62.0 采用 [MIT 许可证](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/LICENSE)，模型服务和其他依赖另有条款。

更多架构差异见 [方案比较](../docs/openhands-comparison.md)。测试命令和部署参数以本目录配置及实际执行结果为准，未执行 Docker / 模型端到端测试不代表联调通过。

## 验证与边界

```bash
make openhands-install
make openhands-lint
make openhands-test
# 独立测试 Docker 宿主机：先构建上面的 agent-server 镜像，再运行
RUN_OPENHANDS_DOCKER_TESTS=1 uv run --project openhands --frozen pytest openhands/tests/test_sdk_integration.py -m docker -v
```

默认测试包括真实 SDK + 本地官方 agent-server + 脚本化模型服务的集成，但没有调用付费模型；Docker 测试需显式开启，未开启显示 skipped。SSE 测试将模型的结束信号暂扣，确认 token 已提前到达客户端，不以最终文本切片冒充流式。

子 Agent 当前继承父 Agent 的 provider 模型和 temperature，不同配置会被拒绝。原生 `finish` 是额外的会话终止工具。每个 Agent 使用原生单工具并发，叶子不能进一步委派，所有请求受总运行超时约束；尚未实现精确的共享模型调用/token 配额。`max_budget` 是 SDK 依赖模型价格信息的估算，不能当作供应商账单硬限额。

控制器为同一 state_dir/deployment_id 加进程锁，启动时回收该部署标签的孤儿容器。清理失败会保留工作区占额并每5秒重试；如果控制器强制终止且一直未重启，容器不会自动到期。部署方须监控残留容器和 Docker 磁盘。不同实例必须使用不同 deployment_id 与工作区；不支持跨机复制调度。
