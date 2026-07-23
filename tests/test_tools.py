"""Tests for tool definitions and execution logic."""

import asyncio
import logging
import shutil

import pytest

from tests.support import install_model_library_stub, reload_module

install_model_library_stub()
tools_module = reload_module("proof_bench.tools")
ToolConfig = tools_module.ToolConfig
cleanup_mcp_client = tools_module.cleanup_mcp_client
cleanup_mcp_clients = tools_module.cleanup_mcp_clients

HAS_LEAN_LSP_MCP = shutil.which("uvx") is not None


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


class TestStatementUpToProof:
    """`_verify` must reconstruct the statement WITHOUT truncating at an internal `:=`
    (a `let ... :=` binding or a named arg like `(ᵜ := ℂ)`). Regression guard for the
    old `formal.split(":=")[0]` behaviour, which made such problems ungradable."""

    def test_simple_statement_unchanged(self):
        formal = "theorem t : 1 + 1 = 2 :="
        assert tools_module._statement_up_to_proof(formal) == "theorem t : 1 + 1 = 2 :="

    def test_let_binding_not_truncated(self):
        formal = "theorem t (h : f) :\n    let Y := Spec R\n    let H := Spec S;\n    P Y H :="
        result = tools_module._statement_up_to_proof(formal)
        assert result == formal
        assert "let H := Spec S" in result
        # the old split-based logic would have cut everything after the first `:=`
        assert result != formal.split(":=")[0].strip() + " :="

    def test_named_argument_not_truncated(self):
        formal = "theorem t {X : Type*} [IsFiniteMeasure (volume (α := X))] : P :="
        assert tools_module._statement_up_to_proof(formal) == formal

    def test_trailing_sorry_stripped(self):
        assert tools_module._statement_up_to_proof("theorem t : P := sorry") == "theorem t : P :="
        assert tools_module._statement_up_to_proof("theorem t : P := by sorry") == "theorem t : P :="

    def test_trailing_admit_stripped(self):
        assert tools_module._statement_up_to_proof("theorem t : P := by admit") == "theorem t : P :="

    def test_let_binding_with_trailing_sorry(self):
        formal = "theorem t : let Y := X; Y = X := by sorry"
        assert tools_module._statement_up_to_proof(formal) == "theorem t : let Y := X; Y = X :="


class TestBuildVerificationCode:
    """Single source of truth for assembling gradeable Lean: header + statement + proof."""

    def test_orders_header_statement_proof(self):
        code = tools_module.build_verification_code("import Mathlib", "theorem t : True :=", "by trivial")
        assert code == "import Mathlib\n\ntheorem t : True :=\nby trivial"

    def test_preserves_let_binding_statement(self):
        formal = "theorem t : let Y := X; Y = X :="
        code = tools_module.build_verification_code("import Mathlib", formal, "by rfl")
        assert "let Y := X" in code
        assert code.endswith(":=\nby rfl")

    def test_strips_trailing_proof_body_from_formal(self):
        code = tools_module.build_verification_code("import Mathlib", "theorem t : True := sorry", "by trivial")
        assert code == "import Mathlib\n\ntheorem t : True :=\nby trivial"


def test_verify_rejects_admit_without_compiling():
    tool = tools_module.SubmitProofTool(
        {"transport": "stdio"},
        {"header": "import Mathlib", "formal": "theorem t : True :="},
    )
    ok, msg = asyncio.run(tool._verify("by admit", logging.getLogger("test")))
    assert ok is False
    assert "admit" in msg.lower()


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

        result = asyncio.run(tools_module.run_lean_code("theorem t : True := by trivial", 30, {"transport": "stdio"}))

        assert result == "ok"
        assert client.calls == 2

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

        result = asyncio.run(tools_module.run_lean_code("theorem t : True := by trivial", 30, {"transport": "stdio"}))

        assert "lake setup-file failed" in result
        assert client.calls == 1


def test_run_lean_code_returns_result(monkeypatch):
    class DummyClient:
        async def call_tool(self, name, arguments):
            assert name == "lean_run_code"
            assert arguments["timeout"] == 30
            return "ok"

    async def fake_get_client(config):
        return DummyClient()

    monkeypatch.setattr(tools_module, "_get_stdio_client", fake_get_client)

    result = asyncio.run(tools_module.run_lean_code("import Mathlib", 30, {"transport": "stdio"}))
    assert result == "ok"


def test_run_lean_code_appends_sorry_warning(monkeypatch):
    class DummyClient:
        async def call_tool(self, name, arguments):
            return "ok"

    async def fake_get_client(config):
        return DummyClient()

    monkeypatch.setattr(tools_module, "_get_stdio_client", fake_get_client)

    result = asyncio.run(tools_module.run_lean_code("theorem t : True := by sorry", 30, {"transport": "stdio"}))
    assert "WARNING: Code contains 'sorry'" in result


def make_mcp_config() -> ToolConfig:
    """Create a fresh ToolConfig for each test."""
    return {
        "transport": "stdio",
        "loogle_local": False,
        "project_path": "/home/ec2-user/proof-bench",
    }


@pytest.mark.skipif(not HAS_LEAN_LSP_MCP, reason="lean-lsp-mcp not available")
class TestRunCodeIntegration:
    """Integration tests for lean_run_code (requires lean-lsp-mcp)."""

    def test_valid_theorem_with_proof(self):
        config = make_mcp_config()
        code = "theorem test_trivial : 1 + 1 = 2 := by rfl"
        result = asyncio.run(tools_module.run_lean_code(code, 30, config))
        assert "sorry" not in result.lower()

    def test_invalid_code_returns_error(self):
        config = make_mcp_config()
        code = "theorem bad : False := by trivial"
        result = asyncio.run(tools_module.run_lean_code(code, 30, config))
        result_lower = result.lower()
        assert "error" in result_lower or "failed" in result_lower or "unsolved" in result_lower


@pytest.mark.skipif(not HAS_LEAN_LSP_MCP, reason="lean-lsp-mcp not available")
class TestLoogleIntegration:
    """Integration tests for lean_loogle (requires lean-lsp-mcp)."""

    def test_search_by_name_returns_results(self):
        config = make_mcp_config()
        result = asyncio.run(tools_module._execute_loogle_query("Nat.add_comm", 3, config))
        assert len(result) > 10

    def test_search_by_substring(self):
        config = make_mcp_config()
        result = asyncio.run(tools_module._execute_loogle_query('"add_comm"', 5, config))
        assert len(result) > 0
