# cloudAgent · OpenHands

基于 **FastAPI + 官方 OpenHands Software Agent SDK / Tools / agent-server 1.62.0** 的配置驱动业务 Agent 服务。本分支仅包含 OpenHands 实现；Deep Agents 和 OpenSandbox 原方案仍保留在 [main](https://github.com/pleb631/cloudAgent/tree/main)，不作为本分支的依赖或启动服务。

用户通过 HTTP 请求让 Agent 分析、读取、修改服务器上已有的文件。服务端将预配置的租户目录直接挂载到执行容器 `/workspace`，文件修改立即作用于原目录，无需上传下载。**去掉的是旧框架，不是执行隔离：命令和文件工具仍在 OpenHands agent-server Docker 容器中运行。**

## Docker 部署

需要 Linux Docker 宿主机和 Docker Compose v2；宿主机不需要安装 Python、uv 或 Agent 依赖。

1. `cp .env.example .env`，设置随机业务 API Key、专用模型 Key、模型地址和租户目录映射。
2. 预先创建 Docker 宿主机上的租户目录，让执行镜像用户能读写。先使用测试目录，不要挂载唯一一份生产数据。
3. 启动：

```bash
docker compose --profile image build
docker compose up -d openhands-api
curl http://127.0.0.1:8001/healthz
```

模型地址必须能从执行容器访问。配置在 `openhands/agents/*.yaml`；API 的 `model` 是配置中的 Agent ID，不是上游模型名。完整操作步骤及权限边界见 [部署与配置](openhands/README.md)。

```bash
curl -N http://127.0.0.1:8001/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_CLOUDAGENT_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'X-Session-ID: demo' \
  -d '{"model":"assistant","messages":[{"role":"user","content":"读取 /workspace 中的资料，在 /workspace/NOTES.md 写一份摘要，不修改其他文件"}],"stream":true}'
```

接口使用 OpenAI Chat Completions 文本子集，但内部会实际调用工具执行任务，不是只返回聊天文本。支持普通 JSON、SSE 和 `/v1/models`；不支持客户端自定义工具或完整 OpenAI 参数集。客户端必须检查流中的 `error` 对象。

## 目录与验证

```text
openhands/         # API、配置、官方 SDK 集成、Docker 生命周期与测试
  agents/          # YAML、系统提示词与技能
  server/          # 固定版本官方镜像上的配置策略入口
compose.yaml       # 默认 OpenHands 部署
.env.example       # 默认部署参数
.github/workflows/openhands.yml
```

```bash
make install       # 本地开发需 Python 3.12+ 和 uv
make lint
make test
make smoke         # 真实 SDK + 本地 agent-server + 脚本化模型
make test-docker   # 显式真实 Docker 集成，须先构建 agent-server 镜像
```

默认测试不调用付费模型，Docker 测试未开启时跳过。测试结果以对应提交的实际运行记录为准，不把替身模型测试或未执行的 Docker 测试视为生产验收。

## 关键限制

- 单实例、单 worker；租户鉴权和并发准入在 API 层。同一租户只允许一个活动请求。
- 每请求新容器与新 Conversation；文件持久，但服务端不保存跨请求对话。客户端自行传入所需 `messages`；`X-Session-ID` 只是关联标签。
- 请求取消或失败不回滚已经写入的宿主文件，必须有备份或版本控制。
- 保留 CPU、内存、PID 限制、超时及容器清理；没有硬磁盘配额和默认禁止出站网络，不适合敌对多租户。
- Docker socket 只给控制器，不给执行容器；控制器因此具有高权限。模型 Key 会进入执行环境，应使用专用、低额度、可撤销 Key。

详见 [架构](docs/architecture.md)、[安全边界](docs/security.md)、[容量与运维](docs/resource-capacity.md) 和 [与主分支方案对比](docs/openhands-comparison.md)。
