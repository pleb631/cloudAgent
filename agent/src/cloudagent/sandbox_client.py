"""Official OpenSandbox SDK adapter. No user code runs in the agent process.

Ownership and execd credentials live only in this process. Restarting loses sessions;
remote TTL remains the fallback cleanup. Paths are lexically workspace-relative, NOT
symlink-confined: the dedicated sandbox container is the security boundary.
"""

import asyncio
import secrets
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import PurePosixPath

import httpx
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import DirectoryListEntry, WriteEntry
from opensandbox.models.sandboxes import NetworkPolicy

MAX_BYTES = 65536
MAX_ENTRIES = 200
MAX_SCAN_FILES = 100
MAX_MATCHES = 100
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class SandboxError(Exception):
    """A safe, user-visible sandbox operation failure."""


class _BoundedResponseStream(httpx.AsyncByteStream):
    """Count raw bytes before HTTPX buffers or parses untrusted execd responses."""

    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    async def __aiter__(self):
        count = 0
        try:
            async for chunk in self.stream:
                count += len(chunk)
                if count > self.limit:
                    raise SandboxError("Sandbox response exceeds size limit")
                yield chunk
        finally:
            await self.stream.aclose()

    async def aclose(self):
        await self.stream.aclose()


class BoundedSandboxTransport(httpx.AsyncBaseTransport):
    """Per-sandbox HTTP transport; limits JSON, files and SSE before SDK parsing.

    Explicit transport bypasses HTTPX environment proxy discovery. Compression is
    forbidden so a small encoded payload cannot become a decompression bomb.
    """

    def __init__(self, inner=None, limit=MAX_RESPONSE_BYTES):
        self.inner = (
            inner
            if inner is not None
            else httpx.AsyncHTTPTransport(
                trust_env=False,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        )
        self.limit = limit
        self.closed = False

    async def handle_async_request(self, request):
        request.headers["Accept-Encoding"] = "identity"
        response = await self.inner.handle_async_request(request)
        try:
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                raise SandboxError("Compressed sandbox responses are not accepted")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    parsed_length = int(length)
                except ValueError:
                    raise SandboxError("Invalid sandbox response length") from None
                if parsed_length < 0 or parsed_length > self.limit:
                    raise SandboxError("Sandbox response exceeds size limit")
            response.stream = _BoundedResponseStream(response.stream, self.limit)
            return response
        except BaseException:
            await response.aclose()
            raise

    async def aclose(self):
        if not self.closed:
            self.closed = True
            await self.inner.aclose()


@dataclass
class _Session:
    sandbox: object
    expires: float
    transport: BoundedSandboxTransport
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class OpenSandboxClient:
    def __init__(
        self,
        domain: str,
        api_key: str,
        *,
        image: str = "python:3.12-slim",
        ttl_seconds: int = 600,
        cpu: str = "1",
        memory: str = "512Mi",
        max_sessions: int = 50,
        max_sessions_per_tenant: int = 10,
        protocol: str = "http",
        factory=None,
    ):
        if not 60 <= ttl_seconds <= 86400 or min(max_sessions, max_sessions_per_tenant) < 1:
            raise ValueError("Invalid sandbox lifetime or capacity")
        if protocol not in {"http", "https"}:
            raise ValueError("Invalid sandbox protocol")
        self.domain, self.api_key = domain, api_key
        self.image, self.ttl = image, timedelta(seconds=ttl_seconds)
        self.cpu, self.memory, self.protocol = cpu, memory, protocol
        self.max_sessions, self.max_tenant = max_sessions, max_sessions_per_tenant
        self.factory = factory or Sandbox.create
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._pending: dict[tuple[str, str], asyncio.Task] = {}
        self._retiring: set[asyncio.Task] = set()
        self._guard = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _path(value) -> str:
        if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
            raise SandboxError("Invalid workspace path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise SandboxError("Path must stay within the workspace")
        return str(PurePosixPath("/workspace") / path)

    @staticmethod
    def _text(value, label: str, maximum: int = MAX_BYTES) -> str:
        if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
            raise SandboxError(f"Invalid or oversized {label}")
        return value

    async def _dispose(self, sandbox, transport):
        # Always attempt both; credentials/errors are never exposed to callers.
        for method in (sandbox.kill, sandbox.close, transport.aclose):
            try:
                async with asyncio.timeout(5):
                    await method()
            except Exception:
                pass

    def _retire(self, entry):
        task = asyncio.create_task(self._dispose(entry.sandbox, entry.transport))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _create(self, key):
        sandbox = None
        transport = BoundedSandboxTransport()
        transferred = False
        try:
            token = secrets.token_urlsafe(32)
            config = ConnectionConfig(
                domain=self.domain,
                api_key=self.api_key,
                protocol=self.protocol,
                use_server_proxy=True,
                headers={"X-EXECD-ACCESS-TOKEN": token},
                request_timeout=timedelta(seconds=35),
                follow_redirects=False,
                transport=transport,
            )
            async with asyncio.timeout(45):
                sandbox = await self.factory(
                    self.image,
                    timeout=self.ttl,
                    ready_timeout=timedelta(seconds=30),
                    resource={"cpu": self.cpu, "memory": self.memory},
                    env={"EXECD_ACCESS_TOKEN": token},
                    network_policy=NetworkPolicy(default_action="deny", egress=[]),
                    connection_config=config,
                )
            async with asyncio.timeout(10):
                await sandbox.files.create_directories([WriteEntry(path="/workspace", mode=755)])
            async with self._guard:
                if self._closed:
                    raise SandboxError("Sandbox client is closed")
                entry = _Session(sandbox, time.monotonic() + self.ttl.total_seconds(), transport)
                self._sessions[key] = entry
                transferred = True
                sandbox = None  # Ownership transferred to session cache.
                return entry
        finally:
            if sandbox is not None:
                await self._dispose(sandbox, transport)
            if not transferred:
                await transport.aclose()
            async with self._guard:
                self._pending.pop(key, None)

    async def _get(self, key):
        async with self._guard:
            if self._closed:
                raise SandboxError("Sandbox client is closed")
            for old_key, old in list(self._sessions.items()):
                if old.users == 0 and old.expires <= time.monotonic():
                    del self._sessions[old_key]
                    self._retire(old)
            if key in self._sessions:
                entry = self._sessions[key]
                entry.users += 1
                return entry
            task = self._pending.get(key)
            if task is None:
                keys = self._sessions.keys() | self._pending.keys()
                if (
                    len(keys) >= self.max_sessions
                    or sum(k[0] == key[0] for k in keys) >= self.max_tenant
                ):
                    raise SandboxError("Sandbox session capacity reached")
                task = asyncio.create_task(self._create(key))
                # Observe failures even when all request waiters were cancelled.
                task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
                self._pending[key] = task
        # Cancellation of a request must not strand a newly created remote container.
        # Creation continues under bounded deadlines, then is cached or closed.
        entry = await asyncio.shield(task)
        async with self._guard:
            if self._closed:
                raise SandboxError("Sandbox client is closed")
            entry.users += 1
            return entry

    def _validate(self, operation, args):
        if operation not in {"execute", "read", "write", "edit", "ls", "grep"} or not isinstance(
            args, dict
        ):
            raise SandboxError("Unsupported sandbox operation")
        if operation == "execute":
            self._text(args.get("command"), "command", 8192)
            timeout = args.get("timeout", 30)
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 30
            ):
                raise SandboxError("Command timeout must be between 0 and 30 seconds")
            return
        self._path(args.get("path", "." if operation in {"ls", "grep"} else None))
        if operation == "write":
            self._text(args.get("content"), "content")
        elif operation == "edit":
            if not self._text(args.get("old"), "old text"):
                raise SandboxError("Old text cannot be empty")
            self._text(args.get("new"), "new text")
        elif operation == "grep" and not self._text(args.get("pattern"), "pattern", 1024):
            raise SandboxError("Pattern cannot be empty")

    async def call(self, tenant_id: str, session_id: str, operation: str, arguments: dict) -> dict:
        entry = None
        try:
            self._validate(operation, arguments)
            if not tenant_id or not session_id:
                raise SandboxError("Missing session identity")
            entry = await self._get((tenant_id, session_id))
            async with entry.lock:
                async with asyncio.timeout(10):
                    await entry.sandbox.renew(self.ttl)
                entry.expires = time.monotonic() + self.ttl.total_seconds()
                async with asyncio.timeout(40):
                    return await self._operate(entry.sandbox, operation, arguments)
        except SandboxError:
            raise
        except Exception:
            # SDK exception strings can contain URLs, headers, or remote payloads.
            raise SandboxError("Sandbox operation failed or timed out") from None
        finally:
            if entry is not None:
                entry.users -= 1

    async def _read(self, sandbox, path):
        data = bytearray()
        async with aclosing(
            sandbox.files.read_bytes_stream(path, chunk_size=8192, limit=MAX_BYTES + 1)
        ) as stream:
            async for chunk in stream:
                if len(data) + len(chunk) > MAX_BYTES:
                    raise SandboxError("File exceeds 64 KiB limit")
                data.extend(chunk)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise SandboxError("File is not UTF-8 text") from None

    async def _write(self, sandbox, path, content):
        self._text(content, "content")
        await sandbox.files.create_directories(
            [WriteEntry(path=str(PurePosixPath(path).parent), mode=755)]
        )
        await sandbox.files.write_files([WriteEntry(path=path, data=content, mode=644)])

    async def _execute(self, sandbox, args):
        command = self._text(args.get("command"), "command", 8192)
        timeout = args.get("timeout", 30)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 30
        ):
            raise SandboxError("Command timeout must be between 0 and 30 seconds")
        output = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = False
        execution_id = None

        async def capture(name, message):
            nonlocal truncated
            raw = message.text.encode("utf-8")
            remaining = MAX_BYTES - len(output[name])
            output[name].extend(raw[:remaining])
            truncated |= len(raw) > remaining

        async def init(event):
            nonlocal execution_id
            execution_id = event.id

        async def stdout(message):
            await capture("stdout", message)

        async def stderr(message):
            await capture("stderr", message)

        try:
            async with asyncio.timeout(timeout + 5):
                result = await sandbox.commands.run(
                    command,
                    opts=RunCommandOpts(
                        working_directory="/workspace", timeout=timedelta(seconds=timeout)
                    ),
                    handlers=ExecutionHandlers(
                        on_init=init, on_stdout=stdout, on_stderr=stderr, skip_accumulation=True
                    ),
                )
        except BaseException:
            if execution_id:
                try:
                    async with asyncio.timeout(3):
                        await sandbox.commands.interrupt(execution_id)
                except Exception:
                    pass
            raise
        return {name: data.decode("utf-8", errors="replace") for name, data in output.items()} | {
            "exit_code": result.exit_code,
            "failed": bool(getattr(result, "error", None)) or result.exit_code != 0,
            "error": "Command failed or did not complete"
            if getattr(result, "error", None) or result.exit_code is None
            else None,
            "truncated": truncated,
        }

    async def _operate(self, sandbox, operation, args):
        if operation == "execute":
            return await self._execute(sandbox, args)
        path = self._path(args.get("path", "." if operation in {"ls", "grep"} else None))
        if operation == "read":
            return {"path": args["path"], "content": await self._read(sandbox, path)}
        if operation == "write":
            await self._write(sandbox, path, args.get("content"))
            return {"path": args["path"], "written": True}
        if operation == "edit":
            old, new = (
                self._text(args.get("old"), "old text"),
                self._text(args.get("new"), "new text"),
            )
            content = await self._read(sandbox, path)
            if not old or content.count(old) != 1:
                raise SandboxError("Edit requires exactly one occurrence of old text")
            await self._write(sandbox, path, content.replace(old, new, 1))
            return {"path": args["path"], "edited": True}
        if operation == "ls":
            entries = await sandbox.files.list_directory(DirectoryListEntry(path=path, depth=1))
            return {
                "entries": [
                    {
                        "path": e.path.removeprefix("/workspace/"),
                        "type": e.entry_type,
                        "size": e.size,
                    }
                    for e in entries[:MAX_ENTRIES]
                ],
                "truncated": len(entries) > MAX_ENTRIES,
            }
        pattern = self._text(args.get("pattern"), "pattern", 1024)
        if not pattern:
            raise SandboxError("Pattern cannot be empty")
        # Literal matching, bounded breadth-first traversal; no regex/shell execution.
        queue, matches, scanned, visited = [path], [], 0, set()
        truncated = False
        while queue and scanned < MAX_SCAN_FILES and len(matches) < MAX_MATCHES:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            info = await sandbox.files.get_file_info([current])
            entry = info[current]
            if entry.entry_type == "directory":
                entries = await sandbox.files.list_directory(
                    DirectoryListEntry(path=current, depth=1)
                )
                for child in entries[:MAX_ENTRIES]:
                    if child.path.startswith("/workspace/") and child.path not in visited:
                        if len(queue) < MAX_ENTRIES:
                            queue.append(child.path)
                        else:
                            truncated = True
                truncated |= len(entries) > MAX_ENTRIES
                if len(visited) >= MAX_ENTRIES:
                    truncated = True
                    break
                continue
            if entry.entry_type != "file" or entry.size > MAX_BYTES:
                continue
            scanned += 1
            try:
                content = await self._read(sandbox, current)
            except SandboxError:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if pattern in line:
                    matches.append(
                        {
                            "path": current.removeprefix("/workspace/"),
                            "line": number,
                            "text": line[:1024],
                        }
                    )
                    if len(matches) >= MAX_MATCHES:
                        truncated = True
                        break
        return {"matches": matches, "truncated": truncated or bool(queue)}

    async def close(self) -> None:
        async with self._guard:
            self._closed = True
            pending = list(self._pending.values())
        # Do not cancel in-flight creation: retain the returned ID for cleanup.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        async with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for entry in sessions:
            async with entry.lock:
                await self._dispose(entry.sandbox, entry.transport)
        if self._retiring:
            await asyncio.gather(*self._retiring)
