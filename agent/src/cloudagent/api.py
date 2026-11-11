"""OpenAI Chat Completions-compatible subset with tenant-bound sessions."""

import asyncio
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from cloudagent.config import AgentRegistry, Settings
from cloudagent.sandbox_client import HttpSandboxClient

logger = logging.getLogger(__name__)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=200_000)


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_usage: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=64)
    messages: list[Message] = Field(min_length=1, max_length=200)
    stream: bool = False
    stream_options: StreamOptions | None = None
    # Accepted for client compatibility, never used as an identity or authorization input.
    user: str | None = Field(default=None, max_length=256)


class RunLimits:
    """Process-local admission control; deploy one worker in this first version."""

    def __init__(self, total: int, per_tenant: int):
        self.total = total
        self.per_tenant = per_tenant
        self.active: set[tuple[str, str]] = set()
        self.lock = asyncio.Lock()

    async def acquire(self, tenant: str, session: str):
        async with self.lock:
            key = (tenant, session)
            if key in self.active:
                raise HTTPException(409, "This session already has an active run")
            if (
                len(self.active) >= self.total
                or sum(t == tenant for t, _ in self.active) >= self.per_tenant
            ):
                raise HTTPException(
                    429, "Concurrent run limit exceeded", headers={"Retry-After": "2"}
                )
            self.active.add(key)

        released = False

        async def release():
            nonlocal released
            async with self.lock:
                if not released:
                    released = True
                    self.active.discard(key)

        return release


class BodyLimitMiddleware:
    """Bound actual received bytes, including requests without Content-Length."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body, size = bytearray(), 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                response = JSONResponse(
                    {
                        "error": {
                            "message": "Request body too large",
                            "type": "invalid_request_error",
                            "code": "body_too_large",
                        }
                    },
                    413,
                )
                return await response(scope, receive, send)
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(
    settings: Settings | None = None, *, registry=None, runtime=None, sandbox=None
) -> FastAPI:
    settings = settings or Settings()
    limits = RunLimits(settings.max_concurrent_runs, settings.max_runs_per_tenant)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.api_keys:
            raise RuntimeError("Set CLOUDAGENT_API_KEYS before starting the service")
        app.state.registry = registry or AgentRegistry.load(settings.agents_dir)
        client = sandbox
        if runtime is None:
            if len(settings.sandbox_token.get_secret_value()) < 32:
                raise RuntimeError("Set CLOUDAGENT_SANDBOX_TOKEN (at least 32 characters)")
            if not settings.llm_api_key.get_secret_value():
                raise RuntimeError("Set CLOUDAGENT_LLM_API_KEY before starting the service")
            from cloudagent.runtime import AgentRuntime

            client = sandbox or HttpSandboxClient(
                settings.sandbox_url, settings.sandbox_token.get_secret_value()
            )
            app.state.runtime = AgentRuntime(
                app.state.registry,
                client,
                llm_base_url=settings.llm_base_url,
                llm_api_key=settings.llm_api_key.get_secret_value(),
                request_timeout=settings.request_timeout,
            )
        else:
            app.state.runtime = runtime
        try:
            yield
        finally:
            if client is not None:
                await client.close()

    app = FastAPI(title="cloudAgent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware, limit=settings.max_request_bytes)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            {
                "error": {
                    "message": exc.detail,
                    "type": "invalid_request_error",
                    "code": str(exc.status_code),
                }
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )

    async def tenant(authorization: Annotated[str | None, Header()] = None) -> str:
        token = (
            authorization.removeprefix("Bearer ")
            if authorization and authorization.startswith("Bearer ")
            else ""
        )
        for key, identity in settings.api_keys.items():
            if hmac.compare_digest(token.encode("utf-8"), key.encode("utf-8")):
                return identity
        raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request, identity: str = Depends(tenant)):
        return {
            "object": "list",
            "data": [
                {"id": agent.id, "object": "model", "created": 0, "owned_by": "cloudagent"}
                for agent in request.app.state.registry.list()
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat(
        body: ChatRequest,
        request: Request,
        identity: str = Depends(tenant),
        x_session_id: Annotated[str | None, Header(pattern=r"^[a-zA-Z0-9_-]{1,64}$")] = None,
    ):
        try:
            request.app.state.registry.get(body.model)
        except KeyError:
            raise HTTPException(404, "Unknown agent model") from None
        session = x_session_id or uuid.uuid4().hex
        release = await limits.acquire(identity, session)
        completion_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        headers = {"X-Session-ID": session, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        def chunk(delta=None, finish=None, *, usage_only=False):
            result = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": body.model,
                "choices": []
                if usage_only
                else [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
            }
            if usage_only:
                result["usage"] = dict(usage)
            return "data: " + json.dumps(result, ensure_ascii=False) + "\n\n"

        async def events():
            async with asyncio.timeout(settings.run_timeout):
                async for event in request.app.state.runtime.stream(
                    [message.model_dump() for message in body.messages],
                    body.model,
                    identity,
                    session,
                ):
                    if event["type"] == "usage":
                        usage["prompt_tokens"] += event.get("prompt_tokens", 0)
                        usage["completion_tokens"] += event.get("completion_tokens", 0)
                        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                    yield event

        async def sse():
            try:
                yield chunk({"role": "assistant"})
                async for event in events():
                    if event["type"] == "token" and event.get("text"):
                        yield chunk({"content": event["text"]})
                yield chunk(finish="stop")
                if body.stream_options and body.stream_options.include_usage:
                    yield chunk(usage_only=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Agent run failed: %s", type(exc).__name__)
                yield 'data: {"error":{"message":"Agent run failed; check service logs and provider availability","type":"server_error","code":"run_failed"}}\n\n'
            finally:
                await release()
            yield "data: [DONE]\n\n"

        if body.stream:
            return StreamingResponse(
                sse(),
                media_type="text/event-stream",
                headers=headers,
                background=BackgroundTask(release),
            )
        try:
            text = []
            async for event in events():
                if event["type"] == "token":
                    text.append(event["text"])
            return JSONResponse(
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": body.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "".join(text)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                },
                headers=headers,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise HTTPException(504, "Agent run timed out") from None
        except Exception as exc:
            logger.warning("Agent run failed: %s", type(exc).__name__)
            raise HTTPException(
                502, "Agent run failed; check service logs and provider availability"
            ) from None
        finally:
            await release()

    return app


app = create_app()
