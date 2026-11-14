# OpenHands 方案与现有方案

本分支仅保留 `openhands/` 实现，并由根目录 `compose.yaml` 默认部署。Deep Agents + OpenSandbox 的代码、依赖、部署和测试已移出本分支，原方案仍保留在 [main 分支](https://github.com/pleb631/cloudAgent/tree/main)。下表是与主分支原方案的架构对照，不表示本分支同时运行两套框架。OpenHands 方案使用官方 Software Agent SDK、Tools 和 agent-server **1.62.0**。

这里接入的是 **Software Agent SDK**，不是部署完整 OpenHands 产品。它不自带完整产品的登录、项目管理、Web UI、计费或长期会话管理。

## 主要差异

| 项目 | 原方案 | OpenHands 方案 |
| --- | --- | --- |
| Agent 执行 | Deep Agents / LangGraph | 官方 SDK Agent / RemoteConversation |
| 执行环境 | 官方 OpenSandbox 服务管理 | Docker SDK 薄生命周期层 + 官方 agent-server |
| 文件 | 临时会话沙箱，重要成果需导出 | 服务器预配置租户目录直接读写挂载 `/workspace` |
| 文件交付 | 沙箱文件 API / 导出流程 | 用户直接在原服务器目录读取成果，无上传入口 |
| 会话 | 同进程、有效期内复用会话工作区 | 每请求新容器、新 Conversation；文件持久，SDK 对话不持久 |
| 子智能体 | Deep Agents 原生子智能体 | 原生 `TaskToolSet` + `AgentDefinition`，只允许配置的叶子 Agent |
| Skills | 原方案 Skills 中间件 | 受信任本地配置转为原生 `AgentContext`；不运行时下载公共技能或插件 |
| 流式输出 | 原方案模型流 | SDK token callback 实际增量，不把完整答案切块冒充流式 |
| 网络策略 | OpenSandbox 出站策略 | 本方案没有出站网络拒绝策略 |

## 为什么不是直接使用 DockerWorkspace

官方 `DockerWorkspace` 能创建 agent-server、挂载目录并建立远程连接，但 1.62.0 没有本方案需要的 CPU、内存、PID 等容器资源限制参数。因此仅用 Docker SDK 管理容器启动和销毁；模型循环、工具执行、子智能体和远程通信仍由官方 SDK / agent-server 承担，不重写 Agent 框架。

API Key 在业务 API 层映射到租户。客户端不能通过请求指定宿主机目录；租户工作区由服务端配置。每个租户同时只允许一个请求运行，避免同一目录并行写入。全局并发上限另行限制。`X-Session-ID` 只是请求关联标签，不是 SDK Conversation 恢复凭证；`messages` 是本次请求传入的历史文本，不是服务端保存的历史。

## 适用范围与代价

适合受信任用户在已有服务器项目目录上执行文件分析、编辑、终端任务和有限委派。不适合直接对公网开放给不可信用户执行任意代码。

- bind mount 修改立即作用于宿主机；取消、失败和删除容器都不会撤销已写入的文件。先做好备份或版本控制，不要挂载唯一一份重要数据。
- 容器共享宿主机内核，不是虚拟机或强多租户安全边界；终端也能读取容器内工作区以外的文件。
- 上游模型 Key 会进入 agent-server 的执行环境。容器中的任意代码可能读取这些凭据；应使用独立、低额度、可撤销 Key。
- 没有硬磁盘配额，也没有禁止出站访问。内存/CPU/PID 限制不能阻止写满宿主机磁盘或网络外传。
- 管理 Docker 的服务持有高权限 Docker socket；不得将 socket、其他租户目录、整个数据父目录挂入执行容器。
- 每次冷启动容器有延迟，SDK 状态不跨请求恢复。后续对话必须重新提交需要的文本历史。

## 官方依据与许可证

官方 SDK 仓库及其 1.62.0 版本采用 MIT 许可证；分发其代码时保留相应版权和许可声明。模型服务、镜像基础系统及其他依赖的许可与费用仍需分别审核。

- [SDK v1.62.0](https://github.com/OpenHands/software-agent-sdk/releases/tag/v1.62.0)
- [MIT LICENSE](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/LICENSE)
- [DockerWorkspace 实现](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/openhands-workspace/openhands/workspace/docker/workspace.py)
- [官方远程执行示例](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/examples/02_remote_agent_server/02_convo_with_docker_sandboxed_server.py)
- [原生子智能体示例](https://github.com/OpenHands/software-agent-sdk/blob/v1.62.0/examples/01_standalone_sdk/42_file_based_subagents.py)

部署与接口示例见 [OpenHands README](../openhands/README.md)。测试通过情况以本次实际运行结果为准；本文不将未执行的 Docker / 模型联调视为已验证。

本分支还包含锁定 1.62.0 的服务端策略入口，关闭默认工作区插件与文件子 Agent 发现；这是为配置白名单提供可预测性，不是防止容器内代码攻击的安全沙箱。其共享模型调用/token预算不等同于原分支，估算费用预算不保证实际账单上限。
