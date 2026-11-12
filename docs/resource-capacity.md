# 单主机容量与故障处置

此部署只支持一个 Agent worker 和一个 API 副本。进程内会话计数不是宿主机资源调度器，也不包括重启前遗留、结果未知的创建请求或通过 OpenSandbox 控制面另外创建的实例。

## 先计算预算，再接入流量

默认每个执行容器 1 CPU / 512 MiB。令 N 为允许的最大会话数：执行容器的内存上限之和是 N × 512 MiB，CPU 上限之和是 N × 1 CPU。CPU 限额不是预留，内存上限也不保证宿主机有对应可用内存。空闲会话仍占资源；降低运行并发不能代替降低会话数。

部署前为以下各项分别留预算，并让总和低于专用执行主机/VM 的可用容量：

- 所有执行容器的 CPU、内存、PID 与磁盘。
- 每个实例的一个 egress sidecar：实测峰值加安全余量，不能按零计算。
- Compose 中 Agent 的 1 GiB / 2 CPU 和 OpenSandbox 服务端的 1 GiB / 1 CPU 上限。
- Docker daemon、操作系统、镜像解包、execd 初始化、日志、其他基础设施及故障恢复余量。
- 创建结果不明、删除失败和进程重启后的残留实例；重新接入流量前检查它们，而不是只相信新的进程计数。

例如 4 个会话的执行容器内存上限之和已经是 2 GiB，另有控制服务合计 2 GiB，尚未计入 sidecar、系统和恢复余量。不能据此认定 4 GiB 主机足够。50 × 512 MiB 则仅执行容器就达到 25 GiB。没有适合所有主机的默认容量，必须根据实际资源和负载降低会话数、每租户上限及并发数。

## 哪些限制真正生效

锁定的 OpenSandbox 1.2.0 Docker 实现将执行实例的 CPU / memory 请求转成 Docker `NanoCpus` / `Memory`，并应用 `docker.pids_limit`。Compose 的 `mem_limit`、`cpus` 和 `pids_limit` 仅作用于两个控制服务，**不约束它们通过宿主机 Docker socket 创建的兄弟执行容器或 sidecar**。

已检查官方安装包 `opensandbox_server/config.py`、`services/docker/container_ops.py` 和 `services/docker/networking.py`：

- `egress.requests` / `egress.limits` 是 Kubernetes 配置，不能用来限制本部署的 Docker sidecar。
- Docker sidecar 没有通过上述路径配置独立 CPU / 内存 / PID 硬上限。
- 当前 Docker 配置路径没有提供 `storage_opt` 磁盘硬配额或可配置的聚合 `cgroup_parent`。不要在 TOML 中加上未经支持的字段后就认为已获得保护。
- 没有每个工作区、容器可写层或日志的磁盘硬配额。文件 API 的大小限制无法阻止 execute 写满磁盘。TTL 和清理重试不是资源硬隔离。

因此，本仓库的默认 Compose **不提供覆盖全部执行资源的聚合硬预算**。若业务要求硬预算，先把整个执行部署放入有明确 CPU / RAM 上限和独立限容磁盘的专用 VM，或采用经验证能覆盖执行容器、sidecar、日志和存储的调度/隔离方案。仅限制 Docker daemon 的服务 cgroup 不代表子容器也受限，必须检查实际 cgroup 层级。VM 的预算保护的是外部宿主机，VM 内部仍可能资源耗尽；这不是每租户磁盘隔离。

## 上线与恢复检查表

1. 给 Docker 数据目录使用独立、限容的文件系统/虚拟磁盘，避免写满操作系统盘；预拉取并审核镜像，核算镜像与可写层空间。配置并验证 Docker 日志轮转，限制日志占用。
2. 用真实 Docker 执行最大会话数、长命令、大输出、写满盘及创建/删除失败测试。使用 `docker inspect` 检查执行容器实际 CPU / memory / PID 配置；单独检查 sidecar。观察 `docker stats`、宿主机可用内存、磁盘空间和 inode，不能用单元测试替代。
3. 根据实测保留恢复余量，配置宿主机监控和告警。设置保守的停止接收新请求阈值（例如资源预算达到 80%，并按负载调整）；该停流措施由运维/入口实现，仓库没有自动容量探测或磁盘压力熔断。
4. 出现内存/磁盘压力、清理积压、OpenSandbox 不可用或反复创建结果未知时，先在入口拒绝新请求，必要时停止 Agent。不要通过增加配额或连续重启恢复流量。
5. 在受信任控制面和 Docker 侧核对实际实例、sidecar、TTL、磁盘和日志。确认归属后用官方生命周期 API 清理残留；不要对共享主机运行无差别的 prune。服务端异常时 TTL 清理不能保证发生。
6. 确认资源回到安全预算、清理成功、真实集成检查通过后再恢复流量。若无法验证 sidecar/磁盘的隔离效果，不要开放给公网不可信代码。

修改根 `.env` 后需重新创建 Agent 容器才能更新其环境，例如 `docker compose up -d --force-recreate agent`。`docker compose config` 可检查变量展开，但输出可能包含密钥，不要上传或贴入工单。直接运行 Agent 时使用 `agent/.env.example` 中的配置。
