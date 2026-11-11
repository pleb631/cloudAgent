# Official OpenSandbox deployment

This directory is an independent deployment project for Alibaba-origin
[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox). It contains no custom
sandbox manager API, command runner, or SDK reimplementation. `Dockerfile` installs
the unmodified official `opensandbox-server==1.2.0` from the checked-in `uv.lock`.
The agent uses the official Python SDK in its own independent project.

## Pinned components

- Lifecycle server: Python distribution `opensandbox-server==1.2.0`.
- Execution daemon: `opensandbox/execd:release-1.2.0`.
- Network policy sidecar: `opensandbox/egress:release-1.2.0`.
- Execution environment: configurable `CLOUDAGENT_SANDBOX_IMAGE`, initially
  `python:3.12-slim`. There is no custom runtime build or additional language stack.
  This runtime tag is mutable; select a trusted digest before a controlled deployment.
  Official component release tags are version-pinned, not digest-pinned.

Configuration is based on the
[release-1.2.0 schema](https://github.com/opensandbox-group/OpenSandbox/blob/release-1.2.0/server/configuration.md),
not fields copied from the moving main branch. The official release publishes
coordinated component tags even though its example configuration references older
individual component tags.

## Start on a dedicated Linux Docker host

Use a disposable or dedicated Linux host with Docker Engine, Docker Compose v2,
and nftables support. This Compose setup is not a portable Docker Desktop recipe.
From the repository root:

```sh
cp .env.example .env
# Set user-facing API keys, the LLM API key, and a separate random
# OPENSANDBOX_SERVER_API_KEY (for example: openssl rand -hex 32).
docker pull opensandbox/execd:release-1.2.0
docker pull opensandbox/egress:release-1.2.0
docker pull python:3.12-slim
docker compose up --build -d --wait
```

Only the trusted official server receives `/var/run/docker.sock`. Neither the agent
nor execution containers receive it. Both trusted services use host networking and
explicitly bind loopback: agent `127.0.0.1:8000`, server `127.0.0.1:8090`.
Execution containers do **not** use the host network. OpenSandbox uses Docker bridge
networking and publishes execution ports only on `127.0.0.1` (range 40000–41000).
The agent reaches execution services through the authenticated server proxy.
Do not change these bind addresses to public interfaces. A remote deployment needs
an authenticated TLS entry point and an explicit access/network design.

`OPENSANDBOX_SERVER_API_KEY` is required by Compose and passed using the upstream
configuration environment variable. The lifecycle server expects the
`OPEN-SANDBOX-API-KEY` header. Health/docs routes are public by upstream design.
Do not enable `OPENSANDBOX_INSECURE_SERVER`.

For server-only operation, use `docker compose up --build -d --wait sandbox`.
Independent dependency/schema checks from this directory are:

```sh
uv sync --frozen --group dev
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

## Isolation boundaries and limits

- The agent requests a separate sandbox per tenant/session, CPU/memory limits,
  expiry, and `NetworkPolicy(default_action="deny", egress=[])` using the official SDK.
  The server config sets `egress.mode="dns+nft"` and disables IPv6. DNS-only mode is
  **not** sufficient to block direct-IP traffic. The policy is a caller requirement,
  not a global server default. Protect the server key: other holders can create
  sandboxes without that policy or with different limits.
- Execution containers drop all capabilities, set `no_new_privileges`, retain the
  Docker default seccomp policy, and have a PID limit of 256. CPU/memory limits come
  from the SDK create request, not the server's own Compose limits.
- The upstream egress sidecar shares the execution network namespace and needs
  `NET_ADMIN` to enforce nftables rules. It does not inherit execution-container
  resource/PID/cap-drop settings in this upstream release. Treat it as trusted
  infrastructure, not a place to execute user commands.
- The upstream Docker runtime does not provide our previous custom runtime's
  read-only root filesystem or non-root guarantee. The selected image's user and
  writable root filesystem apply. Compose `read_only` hardens the two trusted
  service containers only. Do not describe execution containers as read-only or
  necessarily non-root.
- The default runtime image has Python and a POSIX shell, not a full developer
  workstation. Installing dependencies at runtime is incompatible with deny-all
  egress; use an explicitly selected, prebuilt trusted image when needed.
- The adapter does not request host bind mounts or supply model/API credentials to
  execution containers. The server's host-path allowlist uses a reserved unused
  path because an empty upstream allowlist means unrestricted host paths.
- OpenSandbox's Docker `secure_access=True` feature is not supported; it is a
  Kubernetes gateway feature. The adapter instead sets an individual execd token
  and uses loopback/proxy routing. Local host administrators remain trusted.
- Docker is not VM/microVM isolation. The server's socket grants host-equivalent
  authority, and these controls are not a promise of hostile multi-tenant security.
  Use a dedicated host and evaluate gVisor/Kata or a Kubernetes deployment separately.

## Lifetime, state, and operations

`opensandbox-state` stores upstream SQLite state and metadata, including the
server's writable home directory. Only one server instance should manage this host.
Session execution workspaces are ephemeral; they are not the old custom manager's
persistent named workspaces. Sandbox expiry and explicit SDK cleanup own normal
lifecycle. Restarting the agent does not reattach sessions to old sandboxes.

`docker compose down` stops trusted services; it does not itself terminate containers
created through the Docker API. Before maintenance, stop accepting new work, close
active sessions, and inspect the official lifecycle API/Docker inventory for
remaining sandboxes and sidecars. Check expiry cleanup after server failures.
Do not delete `opensandbox-state` or old volumes without verifying retention needs.
There is no automatic disk quota or durable session-storage guarantee here.

CI's `opensandbox` job builds the server, starts it on a disposable GitHub Actions
Linux Docker host, verifies lifecycle authentication, and runs the agent's real SDK
integration tests. Unit/schema checks and the mocked HTTP smoke test are separate.
A CI definition is not evidence of a successful run; real Docker validation requires
an actual runner with a Docker daemon.
