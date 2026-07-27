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
            "description": f"Timeout in seconds (cannot be more than {MAX_TIMEOUT} seconds).",
            "maximum": MAX_TIMEOUT,
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


# The wall clock is the same cap `_normalize_timeout` already enforces on every Lean
# run, so derive it rather than restating the number.
VERIFICATION_TIMEOUT_SECONDS = MAX_TIMEOUT
VERIFICATION_MAX_HEARTBEATS = 1_000_000

# The axioms Mathlib itself is built on. Anything else in a submission's axiom
# dependencies is an escape hatch rather than a proof -- notably `sorryAx` (from
# `sorry`/`admit`) and `Lean.ofReduceBool`/`Lean.trustCompiler` (from
# `native_decide`, which trusts the compiler and is a known way to "prove" False).
VERIFICATION_AXIOM_ALLOWLIST = frozenset({"propext", "Classical.choice", "Quot.sound"})

# Name of the declaration being graded, for the `#print axioms` probe. Only the
# statement is scanned (never the header), so a header's own lemmas cannot be
# mistaken for the target. Conservative by design: anything unexpected yields None,
# which omits the probe rather than risking a spurious `unknown identifier` error
# that would reject a valid proof. Statement text comes from the dataset, not the
# model, so the model cannot manipulate this.
_DECLARATION_NAME_RE = re.compile(
    r"(?:\A|\n)\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|nonrec\s+)*(?:theorem|lemma)\s+([^\s({\[:⦃⟨]+)"
)

_AXIOM_DEPENDENCY_RE = re.compile(r"depends on axioms:\s*\[([^\]]*)\]")


def _declaration_name(statement: str) -> str | None:
    """Return the name of the theorem/lemma a statement declares, or None."""
    match = _DECLARATION_NAME_RE.search(statement)
    return match.group(1) if match else None


def _disallowed_axioms(result_text: str) -> list[str]:
    """Axioms outside the allowlist reported by `#print axioms`, in order of appearance.

    Lean prints either `'name' does not depend on any axioms` or
    `'name' depends on axioms: [a, b, c]`.
    """
    disallowed: list[str] = []
    for match in _AXIOM_DEPENDENCY_RE.finditer(result_text):
        for axiom in match.group(1).split(","):
            axiom = axiom.strip()
            if axiom and axiom not in VERIFICATION_AXIOM_ALLOWLIST and axiom not in disallowed:
                disallowed.append(axiom)
    return disallowed


def build_verification_code(header: str, formal: str, proof: str, *, include_axiom_check: bool = False) -> str:
    """Assemble the full Lean file that is compiled to grade a submission.

    This is the single source of truth for turning a submission into checkable Lean:
    `header`, then the statement up to its proof-assignment `:=` (see
    `_statement_up_to_proof`), then the model's proof. Both the live grader
    (`SubmitProofTool._verify`) and the batch pipeline (`prover.py`) must build the
    code through this function so the graded code and the logged code cannot drift.

    The heartbeat budget is emitted as a file-level command between header and
    statement -- the only placement that governs elaboration of the whole submitted
    declaration. `set_option ... in` inside a proof body cannot raise the
    declaration-level budget, and submissions have no other way to change it, so the
    budget is part of the benchmark definition: identical for every submission and
    hardware-invariant. Lean's 200k default rejected correct proofs that finished
    well inside the wall-clock limit. The wall clock
    (`VERIFICATION_TIMEOUT_SECONDS`) remains the safety bound for work heartbeats do
    not meter: kernel typechecking, imports, and LSP stalls.

    `include_axiom_check` appends a `#print axioms` probe so the grader can inspect
    what the accepted proof actually rests on (see `_disallowed_axioms`). It is
    opt-in because only the grader needs it: recorded artifacts stay a clean,
    compilable proof file. The statement/proof assembly itself -- the part that can
    drift -- is shared by every caller regardless.
    """
    statement = _statement_up_to_proof(formal)
    code = (
        f"{header}\n\n"
        f"set_option maxHeartbeats {VERIFICATION_MAX_HEARTBEATS}\n\n"
        f"{statement}\n{proof}"
    )
    if include_axiom_check and (name := _declaration_name(statement)):
        code = f"{code}\n\n#print axioms {name}"
    return code


class SubmitProofTool(Tool):
    """Proof submission and verification tool for model_library Agent."""

    name = "submit_proof"
    description = (
        "Submit your final Lean proof for verification. "
        "IMPORTANT: You MUST call this tool to have your proof graded. "
        "Work that is not submitted will NOT be evaluated. "
        "You cannot continue working after calling this tool. "
        f"Verification compiles header + statement + your proof with "
        f"`set_option maxHeartbeats {VERIFICATION_MAX_HEARTBEATS}` (file-level) and a "
        f"{VERIFICATION_TIMEOUT_SECONDS}-second wall-clock limit; `set_option` inside "
        "your proof cannot change these budgets."
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
        state["full_proof_code"] = build_verification_code(
            self._problem_context.get("header", ""),
            self._problem_context.get("formal", ""),
            proof,
        )

        return ToolOutput(output=verify_msg, done=True, error=None if is_valid else verify_msg)

    async def _verify(self, proof: str, logger: logging.Logger) -> tuple[bool, str]:
        """Verify a submitted proof by running it through Lean."""
        if not self._run_code_config:
            return True, "Verification skipped (no run_code_config)"

        header = self._problem_context.get("header", "")
        formal = self._problem_context.get("formal", "")
        if not formal:
            return True, "Verification skipped (no formal statement)"

        if "sorry" in proof.lower():
            return False, "Proof contains 'sorry' - incomplete proof"

        if re.search(r"\badmit\b", proof.lower()):
            return False, "Proof contains 'admit' - incomplete proof"

        full_code = build_verification_code(header, formal, proof, include_axiom_check=True)
        try:
            result_text = await run_lean_code(
                full_code, timeout=VERIFICATION_TIMEOUT_SECONDS, config=self._run_code_config
            )
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

        if disallowed := _disallowed_axioms(result_text):
            return False, (
                f"Proof depends on disallowed axioms: {', '.join(disallowed)}. "
                f"Only {', '.join(sorted(VERIFICATION_AXIOM_ALLOWLIST))} are allowed "
                "(these are what Mathlib itself is built on)."
            )

        return True, "Proof verified successfully"
