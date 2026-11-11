# Sandbox service

Independent Python 3.12 project. Run `uv sync` then set `SANDBOX_TOKEN` (32+ random characters) and run `uv run uvicorn sandbox_service.manager:app --port 8001`. Build execution image with `docker build -f Dockerfile.runtime -t cloudagent-sandbox-runtime:local .`. Only this manager may access the Docker socket. The agent/API must never mount it. Keep manager on a private network with no public ingress; its shared token authorizes all tenants, so the API must authenticate users and establish tenant ownership itself. `/health` is unauthenticated liveness only.

POST `/v1/call`, Bearer token, JSON `{tenant_id, session_id, operation, arguments}` returns the result object directly. Operation arguments:
- execute: command (shell string), timeout (seconds, >0 to 30; default 10). Returns output, exit_code, timed_out, truncated. Output cap 64 KiB.
- read: path. Returns content (UTF-8, maximum 64 KiB).
- write: path, content. Creates parent directories.
- edit: path, old, new. Replaces exactly one occurrence, otherwise fails.
- ls: path (default `.`). Maximum 200 entries.
- grep: pattern (literal substring), path (default `.`). Maximum 200 examined paths/results.

Paths must be relative with no traversal or symlinks. A SHA-256 hash of the tenant/session tuple chooses a separate named Docker volume. Each call uses a new non-root, network-disabled container with read-only root, dropped capabilities, no-new-privileges, memory/CPU/process limits, temporary scratch and a workspace volume. It is forcibly removed after success, failure or timeout. Image must be trusted and prebuilt; manager does not pull/build dynamically. Docker is defense in depth, NOT hardened multi-tenant hostile-code isolation. For untrusted production use, place the manager on a dedicated disposable host or replace Docker with a VM/microVM isolation service. Docker socket access is host-equivalent privilege.

Set `SANDBOX_BACKEND=local` only for development: file operations only, execute is disabled. Protect SANDBOX_LOCAL_ROOT from other writers. Neither backend implements a workspace disk quota. Persistent volumes deliberately survive manager restarts; no automatic TTL deletion occurs. Operators must enforce host disk quotas, workspace admission limits and retention. Inspect `docker volume ls --filter label=cloudagent.workspace=true`; remove specific inactive volumes only after verifying retention requirements. After a manager crash, inspect and remove orphan containers via `docker ps -a --filter label=cloudagent.ephemeral=true`. Run one manager replica per Docker host to retain serialization guarantees. Concurrent calls use bounded admission (default 4), returning HTTP 429 immediately when full; HTTP disconnect does not guarantee immediate remote cancellation, but command duration is bounded and containers are reaped. Manager restart/crash cleanup is operator-managed.

`uv run pytest` runs local/mock tests. `RUN_DOCKER_TESTS=1 uv run pytest` additionally exercises the actual Docker image and daemon; build the image first. Never point integration tests at a production daemon.
