"""Tests for tool definitions and execution logic."""

import asyncio
import json
import shutil

import pytest

from tests.support import install_model_library_stub, reload_module

install_model_library_stub()
tools_module = reload_module("proof_bench.tools")
ToolConfig = tools_module.ToolConfig
cleanup_mcp_client = tools_module.cleanup_mcp_client
cleanup_mcp_clients = tools_module.cleanup_mcp_clients
create_loogle_tool = tools_module.create_loogle_tool
create_run_code_tool = tools_module.create_run_code_tool
create_submission_tool = tools_module.create_submission_tool
execute_loogle_tool = tools_module.execute_loogle_tool
execute_run_code_tool = tools_module.execute_run_code_tool
extract_submission = tools_module.extract_submission

HAS_LEAN_LSP_MCP = shutil.which("uvx") is not None


class MockToolCall:
    """Mock ToolCall for testing."""

    def __init__(self, name: str, args: dict | str):
        self.id = "test-call-id"
        self.name = name
        self.args = args


def _parse_error(result: str, log: dict) -> str:
    parsed = json.loads(result)
    assert "error" in parsed
    assert "error" in log
    return parsed["error"]


@pytest.mark.parametrize(
    ("factory", "config", "expected_name", "required"),
    [
        (create_loogle_tool, {}, "lean_loogle", "query"),
        (create_run_code_tool, {}, "lean_run_code", "code"),
    ],
)
def test_tool_definitions_have_expected_name_and_required_fields(factory, config, expected_name, required):
    tool = factory(config)
    assert tool.name == expected_name
    assert required in tool.body.required


def test_loogle_tool_respects_max_results_config():
    assert create_loogle_tool({"max_results": 15}).body.properties["max_results"]["default"] == 15


@pytest.mark.parametrize(("configured", "expected"), [(None, 8), (0, 1)])
def test_loogle_tool_clamps_max_results_default(configured, expected):
    config: ToolConfig = {} if configured is None else {"max_results": configured}
    assert create_loogle_tool(config).body.properties["max_results"]["default"] == expected


def test_run_code_tool_timeout_is_optional():
    tool = create_run_code_tool({})
    assert "timeout" not in tool.body.required
    assert "timeout" in tool.body.properties


def test_submission_tool_requires_proof_and_mentions_verification():
    tool = create_submission_tool()
    assert tool.name == "submit_proof"
    assert "proof" in tool.body.required
    assert "verification" in tool.body.description.lower()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"proof": "by simp"}, "by simp"),
        (json.dumps({"proof": "by ring"}), "by ring"),
    ],
)
def test_extract_submission_accepts_dict_or_json_string(args, expected):
    result, log = extract_submission(MockToolCall("submit_proof", args))
    assert result == expected
    assert log["submitted"] is True


def test_extract_submission_logs_proof_excerpt():
    long_proof = "by\n" + "  simp\n" * 100
    _, log = extract_submission(MockToolCall("submit_proof", {"proof": long_proof}))
    assert len(log["proof_excerpt"]) <= 400


@pytest.mark.parametrize(
    ("args", "expected_message"),
    [
        ({}, "Missing: proof"),
        ("not valid json {{{", "Invalid arguments"),
    ],
)
def test_extract_submission_returns_errors(args, expected_message):
    result, log = extract_submission(MockToolCall("submit_proof", args))
    assert expected_message in log["error"]
    assert "error" in json.loads(result)


@pytest.mark.parametrize(
    ("args", "expected_fragment"),
    [
        ({"max_results": 5}, "query"),
        ({"query": ""}, "missing: query"),
        ("{{invalid json", "invalid arguments"),
    ],
)
def test_execute_loogle_tool_returns_validation_errors(args, expected_fragment):
    result, log = asyncio.run(execute_loogle_tool(MockToolCall("lean_loogle", args), {"transport": "stdio"}))
    assert expected_fragment in _parse_error(result, log).lower()


def test_execute_loogle_tool_logs_query_arguments(monkeypatch):
    async def fake_execute_loogle_query(query, max_results, config):
        assert query == "Nat.add_comm"
        assert max_results == 8
        assert config == {"transport": "stdio"}
        return "lemma results", "mcp"

    monkeypatch.setattr(tools_module, "_execute_loogle_query", fake_execute_loogle_query)
    result, log = asyncio.run(
        execute_loogle_tool(MockToolCall("lean_loogle", {"query": "Nat.add_comm"}), {"transport": "stdio"})
    )

    assert result == "lemma results"
    assert log["arguments"] == {"query": "Nat.add_comm", "max_results": 8}
    assert log["source"] == "mcp"
    assert log["result_excerpt"] == "lemma results"


@pytest.mark.parametrize(
    ("args", "expected_fragment"),
    [
        ({"timeout": 30}, "code"),
        ({"code": ""}, "missing: code"),
        ("not json", "invalid arguments"),
    ],
)
def test_execute_run_code_tool_returns_validation_errors(args, expected_fragment):
    result, log = asyncio.run(execute_run_code_tool(MockToolCall("lean_run_code", args), {}))
    assert expected_fragment in _parse_error(result, log).lower()


def test_execute_run_code_tool_uses_default_timeout(monkeypatch):
    captured: dict = {}

    async def fake_run_lean_code(code, timeout, config):
        captured.update({"code": code, "timeout": timeout, "config": config})
        return "ok", {"timeout": timeout}

    monkeypatch.setattr(tools_module, "run_lean_code", fake_run_lean_code)

    result, log = asyncio.run(execute_run_code_tool(MockToolCall("lean_run_code", {"code": "#check Nat"}), {"transport": "stdio"}))

    assert result == "ok"
    assert log["timeout"] == tools_module.MAX_TIMEOUT
    assert captured == {
        "code": "#check Nat",
        "timeout": tools_module.MAX_TIMEOUT,
        "config": {"transport": "stdio"},
    }


def test_run_lean_code_logs_code_excerpt(monkeypatch):
    class DummyClient:
        async def call_tool(self, name, arguments):
            assert name == "lean_run_code"
            assert arguments["timeout"] == 30
            return "ok"

    async def fake_get_client(config):
        assert config == {"transport": "stdio"}
        return DummyClient()

    monkeypatch.setattr(tools_module, "_get_stdio_client", fake_get_client)

    code = "import Mathlib\n" * 50
    result, log = asyncio.run(tools_module.run_lean_code(code, 30, {"transport": "stdio"}))

    assert result == "ok"
    assert log["timeout"] == 30
    assert log["code_excerpt"] == code[:200]
    assert log["result_excerpt"] == "ok"


def test_run_lean_code_appends_sorry_warning(monkeypatch):
    class DummyClient:
        async def call_tool(self, name, arguments):
            assert name == "lean_run_code"
            return "ok"

    async def fake_get_client(config):
        return DummyClient()

    monkeypatch.setattr(tools_module, "_get_stdio_client", fake_get_client)

    result, log = asyncio.run(tools_module.run_lean_code("theorem t : True := by sorry", 30, {"transport": "stdio"}))

    assert "WARNING: Code contains 'sorry'" in result
    assert "sorry" in log["code_excerpt"].lower()


class _MockClosableClient:
    def __init__(self):
        self.close_count = 0

    async def aclose(self):
        self.close_count += 1


class TestMcpClientCleanup:
    def test_cleanup_mcp_client_only_closes_target(self):
        tools_module._task_clients.clear()
        first = _MockClosableClient()
        second = _MockClosableClient()
        tools_module._task_clients[101] = first
        tools_module._task_clients[202] = second

        try:
            asyncio.run(cleanup_mcp_client(101))
            assert first.close_count == 1
            assert second.close_count == 0
            assert 101 not in tools_module._task_clients
            assert 202 in tools_module._task_clients
        finally:
            tools_module._task_clients.clear()

    def test_cleanup_mcp_clients_closes_all_cached_clients(self):
        tools_module._task_clients.clear()
        first = _MockClosableClient()
        second = _MockClosableClient()
        tools_module._task_clients[101] = first
        tools_module._task_clients[202] = second

        try:
            asyncio.run(cleanup_mcp_clients())
            assert first.close_count == 1
            assert second.close_count == 1
            assert tools_module._task_clients == {}
        finally:
            tools_module._task_clients.clear()


class TestCommandResolution:
    def test_resolve_stdio_command_prefers_explicit_config(self):
        command = tools_module.resolve_stdio_command({"stdio_command": ["custom", "launcher"]})
        assert command == ["custom", "launcher"]

    def test_resolve_stdio_command_falls_back_to_python3(self, monkeypatch):
        def fake_which(name: str):
            if name == "python3":
                return "/usr/bin/python3"
            return None

        monkeypatch.setattr(tools_module.shutil, "which", fake_which)
        command = tools_module.resolve_stdio_command({})
        assert command == ["/usr/bin/python3", "-m", "lean_lsp_mcp", "--transport", "stdio"]


class TestTimeoutNormalization:
    def test_normalize_timeout_handles_invalid_value(self):
        assert tools_module._normalize_timeout("bad") == tools_module.MAX_TIMEOUT

    def test_normalize_timeout_clamps_low_and_high_values(self):
        assert tools_module._normalize_timeout(0) == 1
        assert tools_module._normalize_timeout(9999) == tools_module.MAX_TIMEOUT


class TestRunLeanCodeRecovery:
    def test_run_lean_code_retries_once_after_successful_recovery(self, monkeypatch):
        class DummyClient:
            def __init__(self):
                self.calls = 0

            async def call_tool(self, name, arguments):
                self.calls += 1
                if self.calls == 1:
                    return "lake setup-file failed: no such file or directory Mathlib.Foo.olean"
                return "ok"

        client = DummyClient()

        async def fake_get_client(config):
            return client

        async def fake_recovery(config):
            return True, "recovered", True

        monkeypatch.setattr(tools_module, "_get_stdio_client", fake_get_client)
        monkeypatch.setattr(tools_module, "_attempt_project_recovery", fake_recovery)

        result, log = asyncio.run(tools_module.run_lean_code("theorem t : True := by trivial", 30, {"transport": "stdio"}))

        assert result == "ok"
        assert client.calls == 2
        assert log["recovery_attempted"] is True
        assert log["recovery_succeeded"] is True
        assert log["retried_after_recovery"] is True

    def test_run_lean_code_skips_retry_when_recovery_fails(self, monkeypatch):
        class DummyClient:
            def __init__(self):
                self.calls = 0

            async def call_tool(self, name, arguments):
                self.calls += 1
                return "lake setup-file failed: no such file or directory Mathlib.Foo.olean"

        client = DummyClient()

        async def fake_get_client(config):
            return client

        async def fake_recovery(config):
            return False, "failed", True

        monkeypatch.setattr(tools_module, "_get_stdio_client", fake_get_client)
        monkeypatch.setattr(tools_module, "_attempt_project_recovery", fake_recovery)

        result, log = asyncio.run(tools_module.run_lean_code("theorem t : True := by trivial", 30, {"transport": "stdio"}))

        assert "lake setup-file failed" in result
        assert client.calls == 1
        assert log["recovery_attempted"] is True
        assert log["recovery_succeeded"] is False
        assert "retried_after_recovery" not in log


def make_mcp_config() -> ToolConfig:
    """Create a fresh ToolConfig for each test."""
    return {
        "transport": "stdio",
        "loogle_local": False,  # Use remote API (local requires compatible oleans)
        "project_path": "/home/ec2-user/proof-bench",
    }


@pytest.mark.skipif(not HAS_LEAN_LSP_MCP, reason="lean-lsp-mcp not available")
class TestRunCodeIntegration:
    """Integration tests for lean_run_code (requires lean-lsp-mcp)."""

    def test_valid_theorem_with_proof(self):
        """A valid theorem with proof should pass."""
        config = make_mcp_config()
        code = "theorem test_trivial : 1 + 1 = 2 := by rfl"
        tool_call = MockToolCall("lean_run_code", {"code": code, "timeout": 30})
        result, _ = asyncio.run(execute_run_code_tool(tool_call, config))
        assert "sorry" not in result.lower()

    def test_invalid_code_returns_error(self):
        """Invalid Lean code should return an error message."""
        config = make_mcp_config()
        code = "theorem bad : False := by trivial"
        tool_call = MockToolCall("lean_run_code", {"code": code, "timeout": 30})
        result, _ = asyncio.run(execute_run_code_tool(tool_call, config))
        result_lower = result.lower()
        assert "error" in result_lower or "failed" in result_lower or "unsolved" in result_lower


@pytest.mark.skipif(not HAS_LEAN_LSP_MCP, reason="lean-lsp-mcp not available")
class TestLoogleIntegration:
    """Integration tests for lean_loogle (requires lean-lsp-mcp with local loogle)."""

    def test_search_by_name_returns_results(self):
        """Search for a known lemma should return results."""
        config = make_mcp_config()
        tool_call = MockToolCall("lean_loogle", {"query": "Nat.add_comm", "max_results": 3})
        result, log = asyncio.run(execute_loogle_tool(tool_call, config))
        assert len(result) > 10
        assert "error" not in log or log.get("result_excerpt")

    def test_search_by_substring(self):
        """Search by substring pattern should work."""
        config = make_mcp_config()
        tool_call = MockToolCall("lean_loogle", {"query": '"add_comm"', "max_results": 5})
        result, _ = asyncio.run(execute_loogle_tool(tool_call, config))
        assert len(result) > 0
