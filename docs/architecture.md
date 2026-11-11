# 架构

```text
OpenAI-compatible client
  │ 用户 API Key + X-Session-ID
  ▼
Agent (FastAPI，单进程)
  ├─ 用户 Key → tenant，运行并发与会话互斥
  ├─ 本地 YAML → system prompt / tools / skills / subagents
  └─ 官方 Deep Agents (LangGraph)
       ├─ OpenAI-compatible model
       ├─ 官方 SkillsMiddleware / SubAgentMiddleware
       ├─ 请求私有的 StateBackend、todo / load_skill
       └─ 受限文件/命令工具
            ▼
官方 OpenSandbox Python SDK
  ├─ tenant/session → 独立实例，容量限制、短期绑定、TTL续期
  ├─ 每实例随机 execd Token，不接受用户传入实例ID
  └─ 官方 OpenSandbox 1.2.0 服务端
       ├─ 生命周期、存储元数据、认证代理
       ├─ Docker + 官方 execd
       └─ 官方 egress (dns+nft)，显式默认deny
```

`sandbox/` 只维护官方服务端安装与配置、镜像版本和部署测试，没有自建管理HTTP API。`agent/` 中的适配器负责业务身份与工具语义，不自行创建Docker容器或实现execd。

## 操作映射

- read：官方 read_bytes_stream，读取量限制。
- write：官方 create_directories + write_files。
- edit：受限读取后精确单次替换，再写回；同会话适配器操作串行。
- ls：官方 list_directory，返回数量限制。
- grep：官方文件元数据/列表/读取组合，有限遍历并进行字面文本匹配，不执行用户正则或shell。
- execute：官方 commands.run、服务端超时、流式输出处理、取消时interrupt。
- task：官方子智能体中间件，子级权限取交集；不另开租户身份。

路径只做字面校验，不提供无竞争的符号链接约束。业务文件位于各自实例的 `/workspace`，但该子目录不是安全边界。

## 生命周期和扩展

会话绑定和配额是进程内的，官方服务端持久化其元数据不等于应用能恢复会话。当前不保存每实例凭据，因此重启后不重新连接旧实例；旧实例最终由TTL回收。没有持久化业务卷。

部署示例是Linux Docker宿主机：Agent和官方服务端使用宿主机网络但仅绑定loopback。执行实例加入独立网络命名空间，execd端口只在loopback发布。SDK使用认证服务端代理，不把数据面端口发给最终用户。该代理路径被上游标为维护模式，后续生产扩展应考虑官方Kubernetes ingress gateway。

后续可接入持久化会话/凭据存储、OIDC和Agent级权限、共享配额/锁、审计、人工审批、业务存储和更强的gVisor/Kata/microVM边界。这些不是当前版本已有功能。
