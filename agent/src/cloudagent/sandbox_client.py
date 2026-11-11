"""Narrow RPC client. The agent service never receives a Docker socket."""

import httpx


class SandboxError(Exception):
    """A safe, user-visible sandbox operation failure."""


class HttpSandboxClient:
    def __init__(self, base_url: str, token: str):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(75, connect=5),
            trust_env=False,
        )

    async def call(self, tenant_id: str, session_id: str, operation: str, arguments: dict) -> dict:
        try:
            response = await self.client.post(
                "/v1/call",
                json={
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "operation": operation,
                    "arguments": arguments,
                },
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise SandboxError("Invalid sandbox response")
            return result
        except httpx.HTTPStatusError as exc:
            raise SandboxError(
                f"Sandbox rejected operation (HTTP {exc.response.status_code})"
            ) from None
        except (httpx.HTTPError, ValueError):
            raise SandboxError("Sandbox is unavailable") from None

    async def close(self) -> None:
        await self.client.aclose()
