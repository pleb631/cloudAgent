# OpenHands 单主机架构

本分支只有 OpenHands 实现，不运行 Deep Agents 或 OpenSandbox 服务。

```text
HTTP 客户端 → FastAPI（Bearer Key → 租户、请求校验、并发准入）
                    ↓
       OpenHands SDK / RemoteConversation
                    ↓
   每请求独立的 agent-server Docker 容器
   官方模型循环 / 工具 / 技能 / 叶子子智能体
                    ↓
       /workspace → 预配置租户宿主机目录（读写）
```

控制器通过 Docker SDK 薄生命周期层创建和销毁容器，通过官方 RemoteWorkspace 与 agent-server 通信。没有另写 Agent 推理循环。选择薄生命周期层是为显式设置 CPU、内存、PID 限制，而不是去除沙箱。

- `openhands/src/cloudagent_oh/api.py`：OpenAI 文本子集、SSE、鉴权和请求准入。
- `config.py`：受信任 YAML、提示词、技能、工具及子 Agent 配置；服务端租户目录映射。
- `runtime.py`：官方 SDK 转换、每请求容器、取消/超时和资源回收。
- `openhands/server/`：固定 SDK 版本的配置策略入口，关闭工作区自动插件和文件子 Agent 发现。

控制器只读挂载数据父目录以校验路径；每个执行容器只读写挂载对应租户根目录。终端用户不能选择任意宿主路径。Docker socket 不进入执行容器。

同租户并发返回 409，全局请求限额返回 429。仅支持一个控制器实例和一个 worker；进程锁不是分布式锁。每次请求创建新 Conversation，复用 session ID 不会恢复历史。

文件改动立即持久化；取消只终止运行和回收容器，不撤销写入。清理失败保留占额并重试，启动时回收同部署标签的孤儿容器；控制器不再启动时需运维处理残留。

完整接口、启动和配置示例见 [OpenHands README](../openhands/README.md)。
