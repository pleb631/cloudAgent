"""Private RPC service. Never expose this service to public ingress."""

import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import anyio
from fastapi import FastAPI, HTTPException, Request
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .backend import DockerSandbox, LocalSandbox
from .models import Call


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SANDBOX_")
    token: str = Field(min_length=32)
    backend: Literal["docker", "local"] = "docker"
    image: str = "cloudagent-sandbox-runtime:local"
    local_root: Path = Path("/tmp/cloudagent-workspaces")
    max_concurrent: int = Field(default=4, ge=1, le=32)


def create_app(settings: Settings | None = None, backend=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings()
        app.state.config = config
        app.state.backend = backend or (
            LocalSandbox(config.local_root)
            if config.backend == "local"
            else DockerSandbox(config.image)
        )
        app.state.limiter = anyio.CapacityLimiter(config.max_concurrent)
        try:
            yield
        finally:
            app.state.backend.close()

    app = FastAPI(
        title="Private Sandbox Manager",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/call")
    async def call(request: Request):
        expected = "Bearer " + request.app.state.config.token
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            raise HTTPException(401, "invalid internal credentials")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 262144:
                raise HTTPException(413, "request too large")
        try:
            parsed = Call.model_validate_json(data)
        except ValidationError:
            raise HTTPException(422, "invalid sandbox call") from None
        limiter = request.app.state.limiter
        try:
            limiter.acquire_nowait()
        except anyio.WouldBlock:
            raise HTTPException(429, "sandbox capacity exhausted; retry later") from None
        try:
            # Reject excess work rather than accumulating a remote queue. Keep the
            # permit until the bounded worker finishes, even on client cancellation.
            with anyio.CancelScope(shield=True):
                return await anyio.to_thread.run_sync(
                    request.app.state.backend.call,
                    parsed,
                    abandon_on_cancel=False,
                )
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception:
            raise HTTPException(503, "sandbox runtime failed or timed out") from None
        finally:
            limiter.release()

    return app


app = create_app()
