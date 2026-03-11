"""Tool definitions and MCP execution for Proof Bench."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from model_library.base import ToolBody, ToolDefinition
from model_library.registry_utils import get_registry_model  # noqa: F401 (re-exported)
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from model_library.base import ToolCall

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 90
BOOTSTRAP_TIMEOUT_SECONDS = 600
MCP_INIT_ATTEMPTS = 3
RESULT_EXCERPT_LENGTH = 400


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

    raise RuntimeError(
        "Could not find launcher for lean-lsp-mcp. Expected one of: uvx, python3, or python in PATH."
    )


def _json_error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra})


def _tool_error(message: str) -> tuple[str, dict[str, Any]]:
    return _json_error(message), {"error": message}


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
    return (
        "lake setup-file" in text
        and "no such file or directory" in text
        and ".olean" in text
        and "mathlib" in text
    )


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
        or ("the file worker for" in text and "has been terminated" in text)
        or ("lsp error" in text and "terminated" in text)
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
        pass
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
                logger.warning(f"MCP connection error, invalidating session: {exc}")
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
        logger.debug(f"Removing stale MCP client for task {task_id}")
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
            logger.warning(f"MCP init failed (attempt {attempt + 1}): {e}")
            await asyncio.sleep(2**attempt)

    raise RuntimeError("MCP client init failed")


async def _close_cached_client(task_id: int, client: StdioMCPClient) -> None:
    try:
        await asyncio.wait_for(client.aclose(), timeout=8)
    except TimeoutError:
        logger.warning(f"Timed out closing MCP client {task_id}")
    except Exception as e:
        logger.debug(f"Error closing MCP client {task_id}: {e}")


async def cleanup_mcp_client(task_id: int | None = None) -> None:
    """Close the MCP client for one task."""
    _install_error_handler()

    if task_id is None:
        task_id = _current_task_id()

    client = _task_clients.pop(task_id, None)
    if client is None:
        return

    logger.info(f"Cleaning up MCP client for task {task_id}")
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

    logger.info(f"Cleaning up {len(_task_clients)} MCP client(s)")

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


def _parse_args(tool_call: ToolCall) -> Any:
    """Parse tool call arguments (handles both dict and JSON string)."""
    args = tool_call.args
    if isinstance(args, dict):
        return args
    try:
        return json.loads(args)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"Invalid arguments: {e}") from e


async def _execute_loogle_query(query: str, max_results: int, config: ToolConfig) -> tuple[str, str]:
    daemon_url = config.get("loogle_daemon_url") or os.getenv("LOOGLE_DAEMON_URL")
    if daemon_url:
        try:
            result = await _query_loogle_daemon(daemon_url, query, max_results)
        except Exception as e:
            result = _json_error(f"Daemon query failed: {e}")
        return result, "daemon"

    client = await _get_stdio_client(config)
    result = await client.call_tool("lean_loogle", {"query": query, "max_results": max_results})
    return result, "mcp"


def create_loogle_tool(config: ToolConfig) -> ToolDefinition:
    """Create tool definition for Loogle lemma search."""
    max_results = max(1, config.get("max_results", 8))

    body = ToolBody(
        name="lean_loogle",
        description=(
            "Search Mathlib for lemmas. Use sparingly, only when you need "
            "a specific lemma or to check if a definition/result exists."
        ),
        properties={
            "query": {
                "type": "string",
                "description": (
                    "Query patterns (use ONLY ONE pattern per query, do NOT combine): "
                    '- Substring search: "differ" (with quotes, finds lemmas with "differ" in name); '
                    "- Constant lookup: Real.sin (no quotes, finds lemmas mentioning this constant); "
                    "- Type pattern: _ * (_ ^ _) (with underscores as wildcards); "
                    "- Conclusion pattern: |- tsum _ = _ * tsum _. "
                    "IMPORTANT: Do NOT mix patterns like '\"foo\" bar' - use one pattern type per query."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Max number of search results to return.",
                "default": max_results,
                "minimum": 1,
                "maximum": 20,
            },
        },
        required=["query"],
    )
    return ToolDefinition(name="lean_loogle", body=body)


async def execute_loogle_tool(tool_call: ToolCall, config: ToolConfig) -> tuple[str, dict[str, Any]]:
    """Execute lean_loogle tool call."""
    log_details: dict[str, Any] = {}

    try:
        args = _parse_args(tool_call)
    except ValueError as e:
        return _tool_error(str(e))

    query = args.get("query", "")
    if not query:
        return _tool_error("Missing: query")

    max_results = args.get("max_results", config.get("max_results", 8))
    log_details["arguments"] = {"query": query, "max_results": max_results}
    result, source = await _execute_loogle_query(query, max_results, config)
    log_details["source"] = source

    result = _improve_loogle_error(result, query)
    log_details["result_excerpt"] = result[:RESULT_EXCERPT_LENGTH]
    return result, log_details


def create_run_code_tool(config: ToolConfig) -> ToolDefinition:
    """Create tool definition for running Lean code."""
    _ = config

    body = ToolBody(
        name="lean_run_code",
        description=(
            "Execute Lean code and return compilation results with feedback. "
            "Use to check if proofs compile and get errors/warnings on partial attempts."
        ),
        properties={
            "code": {
                "type": "string",
                "description": "Complete Lean code including imports, definitions, and proof.",
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (cannot be more than 90 seconds).",
                "maximum": 90,
            },
        },
        required=["code"],
    )
    return ToolDefinition(name="lean_run_code", body=body)


async def run_lean_code(code: str, timeout: int, config: ToolConfig) -> tuple[str, dict[str, Any]]:
    """Run Lean code via MCP. Used by both tool execution and proof verification."""
    timeout = _normalize_timeout(timeout)
    log_details: dict[str, Any] = {"timeout": timeout, "code_excerpt": code[:200]}

    client = await _get_stdio_client(config)
    result = await client.call_tool("lean_run_code", {"code": code, "timeout": timeout})

    if _is_missing_olean_failure(result):
        recovered, recovery_details, attempted = await _attempt_project_recovery(config)
        log_details["recovery_attempted"] = attempted
        log_details["recovery_succeeded"] = recovered
        log_details["recovery_details_excerpt"] = recovery_details[:RESULT_EXCERPT_LENGTH]
        if recovered:
            result = await client.call_tool("lean_run_code", {"code": code, "timeout": timeout})
            log_details["retried_after_recovery"] = True

    if "sorry" in code.lower():
        result += "\n\n⚠️ WARNING: Code contains 'sorry'. This is not a complete proof."

    log_details["result_excerpt"] = result[:RESULT_EXCERPT_LENGTH]
    return result, log_details


async def execute_run_code_tool(tool_call: ToolCall, config: ToolConfig) -> tuple[str, dict[str, Any]]:
    """Execute lean_run_code tool call."""
    try:
        args = _parse_args(tool_call)
    except ValueError as e:
        return _tool_error(str(e))

    code = args.get("code", "")
    if not code:
        return _tool_error("Missing: code")

    timeout = _normalize_timeout(args.get("timeout", MAX_TIMEOUT))
    return await run_lean_code(code, timeout, config)


def create_submission_tool() -> ToolDefinition:
    """Create tool definition for submitting a proof."""
    body = ToolBody(
        name="submit_proof",
        description=(
            "Submit your final Lean proof for verification. "
            "IMPORTANT: You MUST call this tool to have your proof graded. "
            "Work that is not submitted will NOT be evaluated. "
            "You cannot continue working after calling this tool."
        ),
        properties={
            "proof": {
                "type": "string",
                "description": "Lean proof starting with `by`.",
            },
        },
        required=["proof"],
    )
    return ToolDefinition(name="submit_proof", body=body)


def extract_submission(tool_call: ToolCall) -> tuple[str, dict[str, Any]]:
    """Extract the submitted proof from a submit_proof tool call."""
    log_details: dict[str, Any] = {}

    try:
        args = _parse_args(tool_call)
    except ValueError as e:
        log_details["error"] = str(e)
        return _json_error(str(e)), log_details

    proof = args.get("proof") if isinstance(args, dict) else args if isinstance(args, str) else None

    if not proof:
        log_details["error"] = "Missing: proof"
        return _json_error("Missing: proof"), log_details

    log_details["submitted"] = True
    log_details["proof_excerpt"] = str(proof)[:RESULT_EXCERPT_LENGTH]
    return str(proof), log_details
