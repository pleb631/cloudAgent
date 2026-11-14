"""OpenAI Chat Completions-compatible subset with tenant-bound sessions."""

import asyncio
import hmac
import json
import logging
import time
import uuid
from contextlib import aclosing, asynccontextmanager
from typing import Annotated, Literal

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from cloudagent_oh.config import AgentRegistry, Settings

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
            key = (tenant, tenant)
            if key in self.active:
                raise HTTPException(409, "This tenant workspace already has an active run")
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


async def finish_request(tasks, release, iterator=None):
    """Join runtime cleanup before returning its admission lease.

    A timeout may already have cancelled the runtime. Do not cancel it again
    while it is awaiting its finally blocks. A separate shielded cleanup task
    also defers repeated explicit request cancellation until cleanup finishes.
    """

    async def cleanup():
        try:
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if iterator is not None:
                await iterator.aclose()
        finally:
            await release()

    cleanup_task = asyncio.create_task(cleanup())
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        cleanup_task.result()
    if cancelled:
        raise asyncio.CancelledError


class ManagedStreamingResponse(StreamingResponse):
    """Own the iterator and admission lease, even when ASGI send fails.

    Background tasks are not guaranteed to run after disconnects. Explicitly close
    the entire iterator chain before releasing admission, without relying on GC.
    """

    def __init__(self, content, *, release, **kwargs):
        super().__init__(content, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        # Use edge-triggered asyncio cancellation rather than the repeated
        # cancellation of Starlette's legacy AnyIO task group: runtime finally
        # blocks must be able to await cleanup after receiving CancelledError.
        stream = asyncio.create_task(self.stream_response(send))
        disconnect = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait((stream, disconnect), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except OSError as exc:
            raise ClientDisconnect() from exc
        finally:
            await finish_request((stream, disconnect), self.release, self.body_iterator)
        if self.background is not None:
            await self.background()


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


def create_app(settings: Settings | None = None, *, registry=None, runtime=None) -> FastAPI:
    settings = settings or Settings()
    limits = RunLimits(settings.max_concurrent_runs, settings.max_runs_per_tenant)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.api_keys:
            raise RuntimeError("Set CLOUDAGENT_OH_API_KEYS before starting the service")
        app.state.registry = registry or AgentRegistry.load(settings.agents_dir)
        missing = set(settings.api_keys.values()) - set(settings.workspace_roots)
        if missing:
            raise RuntimeError("Configure a workspace root for every API-key tenant")
        if runtime is None:
            if not settings.llm_api_key.get_secret_value():
                raise RuntimeError("Set CLOUDAGENT_OH_LLM_API_KEY before starting the service")
            from cloudagent_oh.runtime import OpenHandsRuntime

            app.state.runtime = OpenHandsRuntime(settings, app.state.registry)
        else:
            app.state.runtime = runtime
        active_runtime = app.state.runtime
        try:
            if runtime is None:
                await active_runtime.start()
            yield
        finally:
            await active_runtime.close()

    app = FastAPI(title="cloudAgent OpenHands", version="0.1.0", lifespan=lifespan)
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
            output_bytes = 0
            async with (
                asyncio.timeout(settings.run_timeout),
                aclosing(
                    request.app.state.runtime.stream(
                        [message.model_dump() for message in body.messages],
                        body.model,
                        identity,
                        session,
                    )
                ) as stream,
            ):
                async for event in stream:
                    if event["type"] == "token":
                        output_bytes += len(event.get("text", "").encode("utf-8"))
                        if output_bytes > settings.max_output_bytes:
                            raise RuntimeError("Output limit exceeded")
                    if event["type"] == "usage":
                        usage["prompt_tokens"] += event.get("prompt_tokens", 0)
                        usage["completion_tokens"] += event.get("completion_tokens", 0)
                        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                    yield event

        async def sse():
            try:
                yield chunk({"role": "assistant"})
                async with aclosing(events()) as stream:
                    async for event in stream:
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
            yield "data: [DONE]\n\n"

        if body.stream:
            return ManagedStreamingResponse(
                sse(),
                media_type="text/event-stream",
                headers=headers,
                release=release,
            )

        async def collect():
            text = []
            async with aclosing(events()) as stream:
                async for event in stream:
                    if event["type"] == "token":
                        text.append(event["text"])
            return text

        async def disconnected():
            # The request body has already been consumed by FastAPI. Only this
            # task reads receive while the nonstreaming run is in progress.
            while (await request.receive())["type"] != "http.disconnect":
                pass

        run = asyncio.create_task(collect())
        disconnect = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((run, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                disconnect.result()
                # There is no peer left to receive this response. Returning keeps
                # a normal disconnect out of the server's exception logs.
                return Response(status_code=499)
            text = run.result()
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
            await finish_request((run, disconnect), release)

    return app


app = create_app()
