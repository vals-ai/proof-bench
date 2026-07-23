"""Tool subclasses for model_library Agent."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from model_library.agent import Tool, ToolOutput

from .mcp_client import (
    MAX_TIMEOUT,
    ToolConfig,
    _execute_loogle_query,
    _improve_loogle_error,
    _json_error,
    _normalize_timeout,
    run_lean_code,
)


class LoogleTool(Tool):
    """Loogle lemma search tool for model_library Agent."""

    name = "lean_loogle"
    description = (
        "Search Mathlib for lemmas. Use sparingly, only when you need "
        "a specific lemma or to check if a definition/result exists."
    )
    parameters = {
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
            "default": 8,
            "minimum": 1,
            "maximum": 20,
        },
    }
    required = ["query"]

    def __init__(self, config: ToolConfig):
        super().__init__()
        self._config = config
        self._default_max_results = max(1, config.get("max_results", 8))
        self._is_local = bool(
            config.get("loogle_local")
            or config.get("loogle_daemon_url")
            or os.getenv("LEAN_LOOGLE_LOCAL", "").lower() == "true"
            or os.getenv("LOOGLE_DAEMON_URL")
        )

    async def execute(self, args: dict[str, Any], state: dict[str, Any], logger: logging.Logger) -> ToolOutput:
        query = args.get("query", "")
        if not query:
            return ToolOutput(output=_json_error("Missing: query"), error="Missing: query")

        max_results = args.get("max_results", self._default_max_results)
        try:
            result = await _execute_loogle_query(query, max_results, self._config)
            result = _improve_loogle_error(result, query)
        except Exception as e:
            logger.exception("Loogle query failed")
            return ToolOutput(output=_json_error(f"Loogle query failed: {e}"), error=str(e))

        if not self._is_local:
            logger.debug("Rate limiting: sleeping 15s (remote loogle)")
            await asyncio.sleep(15)

        try:
            error = json.loads(result).get("error")
        except (json.JSONDecodeError, AttributeError):
            error = None

        return ToolOutput(output=result, error=error)


class RunCodeTool(Tool):
    """Lean code execution tool for model_library Agent."""

    name = "lean_run_code"
    description = (
        "Execute Lean code and return compilation results with feedback. "
        "Use to check if proofs compile and get errors/warnings on partial attempts."
    )
    parameters = {
        "code": {
            "type": "string",
            "description": "Complete Lean code including imports, definitions, and proof.",
        },
        "timeout": {
            "type": "number",
            "description": "Timeout in seconds (cannot be more than 90 seconds).",
            "maximum": 90,
        },
    }
    required = ["code"]

    def __init__(self, config: ToolConfig):
        super().__init__()
        self._config = config

    async def execute(self, args: dict[str, Any], state: dict[str, Any], logger: logging.Logger) -> ToolOutput:
        code = args.get("code", "")
        if not code:
            return ToolOutput(output=_json_error("Missing: code"), error="Missing: code")

        timeout = _normalize_timeout(args.get("timeout", MAX_TIMEOUT))
        try:
            result = await run_lean_code(code, timeout, self._config)
        except Exception as e:
            logger.exception("Run code failed")
            return ToolOutput(output=_json_error(f"Run code failed: {e}"), error=str(e))

        return ToolOutput(output=result)


def _statement_up_to_proof(formal: str) -> str:
    """Return the theorem statement up to and including its proof-assignment `:=`.

    The dataset's `formal` field is the full statement, ending at the `:=` that
    introduces the proof body. Any earlier `:=` belong to the statement itself --
    e.g. `let x := ...` bindings or named arguments like `(ᵜ := ℂ)` -- and must be
    preserved. The previous implementation used `formal.split(":=")[0]`, which cut at
    the FIRST `:=` and silently truncated every such statement into uncompilable Lean,
    so those problems could never be graded as resolved regardless of the submission.
    """
    stmt = formal.strip()
    # Drop a trailing bare `sorry`/`admit` proof body if the dataset included one
    # (e.g. `... := sorry`, `... := by sorry`, `... := by admit`).
    stmt = re.sub(r"(:=)\s*(?:by\s+)?(?:sorry|admit)\s*$", r"\1", stmt, flags=re.IGNORECASE).strip()
    if stmt.endswith(":="):
        return stmt
    # Fallback: keep everything up to and including the LAST `:=` (never the first).
    marker = stmt.rfind(":=")
    return stmt[: marker + 2].rstrip() if marker != -1 else f"{stmt} :="


class SubmitProofTool(Tool):
    """Proof submission and verification tool for model_library Agent."""

    name = "submit_proof"
    description = (
        "Submit your final Lean proof for verification. "
        "IMPORTANT: You MUST call this tool to have your proof graded. "
        "Work that is not submitted will NOT be evaluated. "
        "You cannot continue working after calling this tool."
    )
    parameters = {
        "proof": {
            "type": "string",
            "description": "Lean proof starting with `by`.",
        },
    }
    required = ["proof"]

    def __init__(self, run_code_config: ToolConfig | None, problem_context: dict[str, str]):
        super().__init__()
        self._run_code_config = run_code_config
        self._problem_context = problem_context

    async def execute(self, args: dict[str, Any], state: dict[str, Any], logger: logging.Logger) -> ToolOutput:
        proof = args.get("proof")
        if not proof:
            return ToolOutput(output=_json_error("Missing: proof"), error="Missing: proof")

        proof = str(proof)
        is_valid, verify_msg = await self._verify(proof, logger)

        state["proof_text"] = proof
        state["verified"] = is_valid
        state["verify_message"] = verify_msg

        return ToolOutput(output=verify_msg, done=True, error=None if is_valid else verify_msg)

    async def _verify(self, proof: str, logger: logging.Logger) -> tuple[bool, str]:
        """Verify a submitted proof by running it through Lean."""
        if not self._run_code_config:
            return True, "Verification skipped (no run_code_config)"

        header = self._problem_context.get("header", "")
        formal = self._problem_context.get("formal", "")
        if not formal:
            return True, "Verification skipped (no formal statement)"

        formal_clean = _statement_up_to_proof(formal)

        if "sorry" in proof.lower():
            return False, "Proof contains 'sorry' - incomplete proof"

        if re.search(r"\badmit\b", proof.lower()):
            return False, "Proof contains 'admit' - incomplete proof"

        full_code = f"{header}\n\n{formal_clean}\n{proof}"
        try:
            result_text = await run_lean_code(full_code, timeout=90, config=self._run_code_config)
        except Exception as e:
            logger.exception("Verification failed")
            return False, f"Verification error: {e}"

        result_lower = result_text.lower()
        if "error" in result_lower:
            return False, f"Lean error: {result_text[:500]}"
        if "unsolved goals" in result_lower:
            return False, f"Proof incomplete: {result_text[:500]}"
        if "uses 'sorry'" in result_lower:
            return False, f"Proof uses sorry/admit: {result_text[:500]}"

        return True, "Proof verified successfully"
