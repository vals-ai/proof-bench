"""MCP client infrastructure for Proof Bench."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import weakref
from pathlib import Path
from typing import Any, Literal, TypedDict

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)


class _SuppressLSPCleanupFilter(logging.Filter):
    """Drop noisy LSP file-worker termination messages from the mcp logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()
        return not ("the file worker for" in msg and "has been terminated" in msg)


logging.getLogger("mcp").addFilter(_SuppressLSPCleanupFilter())

MAX_TIMEOUT = 90
BOOTSTRAP_TIMEOUT_SECONDS = 600
MCP_INIT_ATTEMPTS = 3


class ToolConfig(TypedDict, total=False):
    """Configuration for stdio MCP transport."""

    transport: Literal["stdio"]
    project_path: str | None
    token: str | None
    log_level: Literal["INFO", "WARNING", "ERROR", "NONE"] | None
    stdio_command: list[str]
    loogle_local: bool
    loogle_daemon_url: str | None
    max_results: int
    client: Any


_task_clients: dict[int, StdioMCPClient] = {}
_loop_exception_handlers: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = weakref.WeakKeyDictionary()
_project_recovery_locks: dict[str, asyncio.Lock] = {}
_project_recovery_attempted: set[str] = set()
_project_recovery_succeeded: set[str] = set()


def _current_task_id() -> int:
    task = asyncio.current_task()
    return id(task) if task else 0


def resolve_stdio_command(config: ToolConfig | None = None) -> list[str]:
    """Resolve a portable command for launching lean-lsp-mcp."""
    if config and config.get("stdio_command"):
        command = list(config["stdio_command"])
        if command:
            return command

    if uvx := shutil.which("uvx"):
        return [uvx, "lean-lsp-mcp", "--transport", "stdio"]

    if python3 := shutil.which("python3"):
        return [python3, "-m", "lean_lsp_mcp", "--transport", "stdio"]

    if python := shutil.which("python"):
        return [python, "-m", "lean_lsp_mcp", "--transport", "stdio"]

    raise RuntimeError("Could not find launcher for lean-lsp-mcp. Expected one of: uvx, python3, or python in PATH.")


def _json_error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


def _loogle_local_enabled(config: ToolConfig) -> bool:
    return config.get("loogle_local", False) or os.getenv("LEAN_LOOGLE_LOCAL", "").lower() == "true"


def _prepare_stdio_command(config: ToolConfig, loogle_local: bool) -> list[str]:
    command = resolve_stdio_command(config)
    if loogle_local and "--loogle-local" not in command:
        return [*command, "--loogle-local"]
    return command


def _build_client_env(config: ToolConfig, loogle_local: bool) -> dict[str, str] | None:
    env: dict[str, str] = {}
    if project_path := config.get("project_path"):
        env["LEAN_PROJECT_PATH"] = str(Path(project_path))
    if token := config.get("token"):
        env["LEAN_LSP_MCP_TOKEN"] = token
    if log_level := config.get("log_level"):
        env["LEAN_LOG_LEVEL"] = str(log_level)
    if loogle_local:
        env["LEAN_LOOGLE_LOCAL"] = "true"
    return env or None


def _normalize_timeout(timeout: Any, default: int = MAX_TIMEOUT) -> int:
    """Parse timeout value safely and clamp to [1, MAX_TIMEOUT]."""
    if timeout is None:
        return default

    try:
        parsed = int(float(timeout))
    except (TypeError, ValueError):
        return default

    if parsed < 1:
        return 1
    return min(parsed, MAX_TIMEOUT)


def _project_key(config: ToolConfig) -> str:
    project_path = config.get("project_path")
    if project_path:
        return str(Path(project_path).expanduser().resolve())
    return str(Path.cwd())


def _is_missing_olean_failure(result_text: str) -> bool:
    text = result_text.lower()
    return "lake setup-file" in text and "no such file or directory" in text and ".olean" in text and "mathlib" in text


async def _run_bootstrap_command(command: list[str], cwd: str) -> tuple[bool, str]:
    """Run a bootstrap command and capture output."""
    started_at = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=BOOTSTRAP_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return False, f"timeout after {BOOTSTRAP_TIMEOUT_SECONDS}s running: {' '.join(command)}"

    elapsed_s = round(time.perf_counter() - started_at, 2)
    combined = (stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace")).strip()
    summary = f"cmd={' '.join(command)} exit={process.returncode} elapsed={elapsed_s}s"
    if combined:
        summary = f"{summary}\n{combined}"

    return process.returncode == 0, summary


async def _attempt_project_recovery(config: ToolConfig) -> tuple[bool, str, bool]:
    """Attempt one-time Lean dependency bootstrap for a project."""
    key = _project_key(config)
    lock = _project_recovery_locks.setdefault(key, asyncio.Lock())

    async with lock:
        if key in _project_recovery_succeeded:
            return True, "recovery already completed for project", False
        if key in _project_recovery_attempted:
            return False, "recovery already attempted for project", False

        _project_recovery_attempted.add(key)
        project_dir = Path(key)
        if not project_dir.exists():
            return False, f"project path does not exist: {project_dir}", True

        try:
            update_ok, update_output = await _run_bootstrap_command(["lake", "update"], cwd=key)
            cache_ok, cache_output = await _run_bootstrap_command(["lake", "exe", "cache", "get"], cwd=key)
            success = update_ok and cache_ok
            details = f"lake update: {update_output}\nlake exe cache get: {cache_output}"
        except Exception as exc:
            return False, f"recovery command failed: {exc}", True

        if success:
            _project_recovery_succeeded.add(key)

        return success, details, True


def _suppress_mcp_cleanup_errors(loop, context):
    """Suppress MCP cancel scope errors during cleanup (non-blocking, cosmetic)."""
    msg = context.get("message", "")
    exc = context.get("exception")
    text = f"{msg}\n{exc}".lower()

    if (
        "cancel scope" in text
        or "future exception was never retrieved" in text
        or "task exception was never retrieved" in text
        or "unhandled errors in a taskgroup" in text
    ):
        return

    previous_handler = _loop_exception_handlers.get(loop)
    if previous_handler is not None and previous_handler is not _suppress_mcp_cleanup_errors:
        previous_handler(loop, context)
    else:
        loop.default_exception_handler(context)


def _install_error_handler():
    """Install asyncio exception handler to suppress MCP cleanup noise."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    current_handler = loop.get_exception_handler()
    if current_handler is _suppress_mcp_cleanup_errors:
        return

    _loop_exception_handlers[loop] = current_handler
    loop.set_exception_handler(_suppress_mcp_cleanup_errors)


class StdioMCPClient:
    """Async MCP client over stdio transport."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        if not command:
            raise ValueError("Command list cannot be empty")

        self._command = command
        if env:
            merged_env = os.environ.copy()
            merged_env.update(env)
            self._env = merged_env
        else:
            self._env = None
        self._session: ClientSession | None = None
        self._read_stream: Any = None
        self._write_stream: Any = None
        self._process: Any = None
        self._lock = asyncio.Lock()
        self._closing = False
        self._owner_task_id: int | None = None

    async def _ensure_session(self) -> None:
        """Lazily initialize the MCP session (spawns subprocess on first call)."""
        if self._session is not None:
            return

        async with self._lock:
            if self._session is not None:
                return

            params = StdioServerParameters(
                command=self._command[0],
                args=self._command[1:],
                env=self._env,
            )

            cm = stdio_client(params)
            transport = await cm.__aenter__()
            self._read_stream, self._write_stream = transport
            self._process = cm

            session = ClientSession(
                read_stream=self._read_stream,
                write_stream=self._write_stream,
            )
            await session.__aenter__()
            await session.initialize()
            self._session = session
            task = asyncio.current_task()
            self._owner_task_id = id(task) if task else 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return the result as a string."""
        try:
            await self._ensure_session()
            assert self._session is not None

            result = await self._session.call_tool(name=name, arguments=arguments)

            if result.isError:
                return json.dumps(
                    {
                        "error": getattr(result, "message", "Unknown MCP error"),
                        "content": [c.model_dump() for c in getattr(result, "content", [])],
                    }
                )

            chunks = []
            for item in result.content:
                text = getattr(item, "text", None)
                if text is not None:
                    chunks.append(text)
                else:
                    try:
                        chunks.append(json.dumps(item.model_dump()))
                    except Exception:
                        chunks.append(str(item))

            return "\n".join(chunks) if chunks else ""

        except Exception as exc:
            if "closed" in str(exc).lower() or "connection" in str(exc).lower():
                logger.warning("MCP connection error, invalidating session: %s", exc)
                self._session = None
            else:
                logger.exception("MCP call failed")
            return json.dumps({"error": str(exc)})

    async def aclose(self) -> None:
        """Best-effort cleanup. Errors suppressed since cleanup may cross task boundaries."""
        if self._closing:
            return
        self._closing = True

        task = asyncio.current_task()
        current_task_id = id(task) if task else 0
        if self._owner_task_id is not None and current_task_id != self._owner_task_id:
            logger.debug(
                "Closing MCP client from different task (owner=%s current=%s)",
                self._owner_task_id,
                current_task_id,
            )

        session = self._session
        process = self._process
        self._session = None
        self._process = None
        self._read_stream = None
        self._write_stream = None
        self._owner_task_id = None

        try:
            await self._safe_aexit(session, "session")
            await self._safe_aexit(process, "transport")
        finally:
            self._closing = False

    async def _safe_aexit(self, resource: Any, label: str, timeout_s: int = 5) -> None:
        if resource is None:
            return
        aexit = getattr(resource, "__aexit__", None)
        if not callable(aexit):
            return
        try:
            await asyncio.wait_for(aexit(None, None, None), timeout=timeout_s)
        except TimeoutError:
            logger.warning("Timed out closing MCP %s", label)
        except Exception as exc:
            logger.debug("Error closing MCP %s: %s", label, exc)

    async def health_check(self) -> tuple[bool, str]:
        """Test connectivity with a simple Lean command."""
        try:
            result = await self.call_tool("lean_run_code", {"code": "#check Nat", "timeout": 30})
            if "error" in result.lower() and "unknown" not in result.lower():
                return False, f"Health check failed: {result[:200]}"
            return True, "MCP server healthy"
        except Exception as e:
            return False, f"Connection failed: {e}"


async def _get_stdio_client(config: ToolConfig) -> StdioMCPClient:
    """Get or create a stdio MCP client."""
    _install_error_handler()

    task_id = _current_task_id()

    if task_id in _task_clients:
        client = _task_clients[task_id]
        if client._session is not None:
            return client
        logger.debug("Removing stale MCP client for task %s", task_id)
        del _task_clients[task_id]

    loogle_local = _loogle_local_enabled(config)
    command = _prepare_stdio_command(config, loogle_local)
    env = _build_client_env(config, loogle_local)

    for attempt in range(MCP_INIT_ATTEMPTS):
        client = StdioMCPClient(command=list(command), env=env)
        try:
            await client._ensure_session()
            _task_clients[task_id] = client
            return client
        except Exception as e:
            await client.aclose()
            if attempt == MCP_INIT_ATTEMPTS - 1:
                raise
            logger.warning("MCP init failed (attempt %d): %s", attempt + 1, e)
            await asyncio.sleep(2**attempt)

    raise RuntimeError("MCP client init failed")


async def _close_cached_client(task_id: int, client: StdioMCPClient) -> None:
    try:
        await asyncio.wait_for(client.aclose(), timeout=8)
    except TimeoutError:
        logger.warning("Timed out closing MCP client %s", task_id)
    except Exception as e:
        logger.debug("Error closing MCP client %s: %s", task_id, e)


async def cleanup_mcp_client(task_id: int | None = None) -> None:
    """Close the MCP client for one task."""
    _install_error_handler()

    if task_id is None:
        task_id = _current_task_id()

    client = _task_clients.pop(task_id, None)
    if client is None:
        return

    logger.info("Cleaning up MCP client for task %s", task_id)
    await _close_cached_client(task_id, client)


async def cleanup_current_task_mcp_client() -> None:
    """Close the MCP client for the current task."""
    await cleanup_mcp_client()


async def cleanup_mcp_clients() -> None:
    """Close all cached MCP clients and their subprocesses."""
    global _task_clients
    _install_error_handler()

    if not _task_clients:
        return

    logger.info("Cleaning up %d MCP client(s)", len(_task_clients))

    clients = list(_task_clients.items())
    _task_clients.clear()

    for task_id, client in clients:
        await _close_cached_client(task_id, client)
    logger.info("MCP client cleanup complete")


async def check_mcp_server(config: ToolConfig | None = None) -> tuple[bool, str]:
    """Verify MCP server is running and responsive."""
    if config is None:
        config = {"transport": "stdio", "loogle_local": True}

    try:
        client = await _get_stdio_client(config)
        return await client.health_check()
    except Exception as e:
        return False, f"Failed to connect: {e}"


async def _query_loogle_daemon(url: str, query: str, max_results: int) -> str:
    """Query the shared loogle daemon via HTTP."""
    import urllib.error
    import urllib.request

    def _do_request() -> str:
        body = json.dumps({"query": query, "max_results": max_results}).encode()
        req = urllib.request.Request(
            f"{url.rstrip('/')}/query",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                return resp.read().decode()
        except urllib.error.URLError as e:
            if "Connection refused" in str(e):
                raise ConnectionError(
                    f"Daemon not running at {url}. Start with: python -m proof_bench.loogle_daemon"
                ) from e
            raise

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do_request)


def _improve_loogle_error(result: str, query: str) -> str:
    """Add helpful hints to loogle error messages about query syntax."""
    try:
        data = json.loads(result)
        if "error" not in data:
            return result

        error = data["error"]
        hints = []

        if "Function expected" in error and "String" in error:
            hints.append(
                "HINT: You're mixing query patterns. Use ONLY ONE pattern per query: "
                'either "substring" (quoted) OR ConstantName (unquoted), not both.'
            )
        if "unexpected end of input" in error.lower():
            hints.append("HINT: Query has unbalanced brackets/parens. Simplify/fix your query.")
        if "Unknown identifier" in error or "unknown identifier" in error:
            if not query.startswith('"'):
                hints.append(
                    f"HINT: '{query.split()[0]}' not found as a constant. Try substring search with quotes: \"keyword\""
                )

        if hints:
            data["hints"] = hints
            return json.dumps(data)
    except (json.JSONDecodeError, KeyError):
        pass
    return result


async def _execute_loogle_query(query: str, max_results: int, config: ToolConfig) -> str:
    daemon_url = config.get("loogle_daemon_url") or os.getenv("LOOGLE_DAEMON_URL")
    if daemon_url:
        try:
            return await _query_loogle_daemon(daemon_url, query, max_results)
        except Exception as e:
            return _json_error(f"Daemon query failed: {e}")

    client = await _get_stdio_client(config)
    return await client.call_tool("lean_loogle", {"query": query, "max_results": max_results})


async def run_lean_code(code: str, timeout: int, config: ToolConfig) -> str:
    """Run Lean code via MCP. Used by both tool execution and proof verification."""
    timeout = _normalize_timeout(timeout)
    client = await _get_stdio_client(config)
    result = await client.call_tool("lean_run_code", {"code": code, "timeout": timeout})

    if _is_missing_olean_failure(result):
        recovered, _, _ = await _attempt_project_recovery(config)
        if recovered:
            result = await client.call_tool("lean_run_code", {"code": code, "timeout": timeout})

    if "sorry" in code.lower():
        result += "\n\nWARNING: Code contains 'sorry'. This is not a complete proof."

    return result
