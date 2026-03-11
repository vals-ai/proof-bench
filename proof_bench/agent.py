from __future__ import annotations

import asyncio
import json
import logging
import os
from types import SimpleNamespace
from typing import Any

from model_library.base import TextInput, ToolResult

from .tools import (
    ToolConfig,
    create_loogle_tool,
    create_run_code_tool,
    create_submission_tool,
    execute_loogle_tool,
    execute_run_code_tool,
    extract_submission,
    get_registry_model,
    run_lean_code,
)

logger = logging.getLogger(__name__)

TOKEN_KEYS = [
    "in_tokens",
    "out_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_input_tokens",
    "total_output_tokens",
]


class LLMAgent:
    """Agent that orchestrates LLM interactions with Lean tools for theorem proving."""

    def __init__(
        self,
        model_str: str,
        loogle_config: ToolConfig | None = None,
        run_code_config: ToolConfig | None = None,
        enable_submission_tool: bool = True,
        max_turns: int = 40,
        problem_context: dict[str, str] | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")

        self.model_str = model_str
        self.loogle_config = loogle_config
        self.run_code_config = run_code_config
        self.enable_submission_tool = enable_submission_tool
        self.max_turns = max_turns
        self.problem_context = problem_context or {}

        self.model = get_registry_model(model_str)
        self.tools = self._build_tools()
        self.turns_used = 0
        self.tool_call_counts: dict[str, int] = {
            "lean_loogle": 0,
            "lean_run_code": 0,
            "submit_proof": 0,
        }
        self.token_usage: dict[str, int] = dict.fromkeys(TOKEN_KEYS, 0)
        self.total_cost: float = 0.0

    async def _query_with_retries(self, query_args: dict[str, Any], max_retries: int = 2) -> Any:
        """Query model with bounded retries for transient failures."""
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return await self.model.query(**query_args)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Model query failed (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                if attempt == max_retries:
                    break
                await asyncio.sleep(2**attempt)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Model query failed without exception details")

    def _turn_status(self) -> str:
        """Returns a human-readable turn status string."""
        remaining = max(0, self.max_turns - self.turns_used)
        if remaining == 0:
            return "[⚠️ FINAL TURN - Call submit_proof NOW or get 0 points!]"
        if remaining == 1:
            return f"[⚠️ Turn {self.turns_used}/{self.max_turns} - NEXT TURN IS LAST! Call submit_proof NOW!]"
        if remaining <= 3:
            return f"[⚠️ Turn {self.turns_used}/{self.max_turns} - ONLY {remaining} TURNS LEFT! Call submit_proof immediately!]"
        if remaining <= 5:
            return f"[Turn {self.turns_used}/{self.max_turns} - {remaining} left. Call submit_proof soon or you will fail!]"
        if remaining <= 10:
            return f"[Turn {self.turns_used}/{self.max_turns} - {remaining} left. Start preparing your submission.]"
        return f"[Turn {self.turns_used}/{self.max_turns}, {remaining} remaining]"

    def get_stats(self) -> dict[str, Any]:
        """Returns summary statistics for this agent run."""
        return {
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
            "total_tool_calls": sum(self.tool_call_counts.values()),
            "tool_call_breakdown": dict(self.tool_call_counts),
            "token_usage": dict(self.token_usage),
            "total_cost": self.total_cost,
        }

    def _build_tool_configs(self) -> dict[str, ToolConfig]:
        tool_configs: dict[str, ToolConfig] = {}
        if self.loogle_config:
            tool_configs["lean_loogle"] = self.loogle_config
        if self.run_code_config:
            tool_configs["lean_run_code"] = self.run_code_config
        if self.enable_submission_tool and self.model.supports_tools:
            tool_configs["submit_proof"] = {}
        return tool_configs

    def _uses_local_loogle(self, config: ToolConfig) -> bool:
        return bool(
            config.get("loogle_local")
            or config.get("loogle_daemon_url")
            or os.getenv("LEAN_LOOGLE_LOCAL", "").lower() == "true"
            or os.getenv("LOOGLE_DAEMON_URL")
        )

    def _record_tool_usage(
        self,
        tool_usage_log: list[dict[str, Any]],
        tool_call: Any,
        log_details: dict[str, Any],
    ) -> None:
        tool_usage_log.append(
            {"turn": self.turns_used, "tool_call_id": tool_call.id, "tool_name": tool_call.name, **log_details}
        )

    def _aggregate_tokens(self, result: Any) -> None:
        """Aggregate token usage from a model query result."""
        metadata = getattr(result, "metadata", None)
        if metadata is None:
            return

        for key in TOKEN_KEYS:
            val = metadata.get(key, 0) if isinstance(metadata, dict) else getattr(metadata, key, 0)
            self.token_usage[key] += val or 0
        cost = metadata.get("cost", 0) if isinstance(metadata, dict) else getattr(metadata, "cost", 0)
        if isinstance(cost, dict):
            self.total_cost += cost.get("total", 0) or 0
        elif isinstance(cost, (int, float)):
            self.total_cost += cost
        elif hasattr(cost, "total"):
            self.total_cost += getattr(cost, "total", 0) or 0

    def _build_tools(self):
        tools = []

        if self.loogle_config and self.model.supports_tools:
            tools.append(create_loogle_tool(self.loogle_config))
        elif self.loogle_config:
            logger.warning("Model %s does not support tools, skipping lean_loogle", self.model_str)

        if self.run_code_config and self.model.supports_tools:
            tools.append(create_run_code_tool(self.run_code_config))
        elif self.run_code_config:
            logger.warning("Model %s does not support tools, skipping lean_run_code", self.model_str)

        if self.enable_submission_tool and self.model.supports_tools:
            tools.append(create_submission_tool())

        return tools

    async def _verify_submission(self, proof: str) -> tuple[bool, str]:
        """Verifies a submitted proof by running it through Lean."""
        if not self.run_code_config:
            return True, "Verification skipped (no run_code_config)"

        header = self.problem_context.get("header", "")
        formal = self.problem_context.get("formal", "")
        if not formal:
            return True, "Verification skipped (no formal statement)"

        formal_clean = formal
        if ":=" in formal_clean:
            formal_clean = formal_clean.split(":=")[0].strip() + " :="

        if "sorry" in proof.lower():
            return False, "Proof contains 'sorry' - incomplete proof"

        full_code = f"{header}\n\n{formal_clean}\n{proof}"
        result_text, _ = await run_lean_code(full_code, timeout=90, config=self.run_code_config)
        result_lower = result_text.lower()

        if "error" in result_lower:
            return False, f"Lean error: {result_text[:500]}"
        if "unsolved goals" in result_lower:
            return False, f"Proof incomplete: {result_text[:500]}"

        return True, "Proof verified successfully"

    async def run(self, query_text: str, system_prompt: str | None = None) -> tuple[Any, list[dict[str, Any]]]:
        """Runs the agent loop until submission or max turns reached."""
        if not query_text or not query_text.strip():
            raise ValueError("Query text cannot be empty")

        tool_usage_log: list[dict[str, Any]] = []

        tool_configs = self._build_tool_configs()

        query_args: dict[str, Any] = {"input": [TextInput(text=query_text)]}
        if system_prompt:
            query_args["system_prompt"] = system_prompt
        if self.tools:
            query_args["tools"] = self.tools

        result = None
        while self.turns_used < self.max_turns:
            self.turns_used += 1
            result = await self._query_with_retries(query_args)
            self._aggregate_tokens(result)

            if not getattr(result, "tool_calls", None):
                query_args["input"] = [
                    TextInput(
                        text="Continue your solution attempt. You can use lean_loogle and lean_run_code tools as needed. When your solution is complete, call submit_proof - your work is only graded if you call this tool, and you cannot continue after calling it. "
                        + self._turn_status()
                    ),
                ]
                query_args["history"] = result.history
                continue

            supported_calls = [tc for tc in result.tool_calls if tc.name in tool_configs][:6]
            if not supported_calls:
                break

            is_final_turn = self.turns_used >= self.max_turns
            if is_final_turn:
                submit_calls = [tc for tc in supported_calls if tc.name == "submit_proof"]
                if submit_calls:
                    supported_calls = submit_calls[:1]
                else:
                    logger.warning("Final turn reached without submit_proof call - forcing end")
                    break

            logger.info("Turn %d: %d tool call(s). %s", self.turns_used, len(supported_calls), self._turn_status())
            tool_results: list[ToolResult] = []

            for tool_call in supported_calls:
                log_details: dict[str, Any] = {}
                result_text = ""
                try:
                    if tool_call.name == "lean_loogle":
                        result_text, log_details = await execute_loogle_tool(tool_call, tool_configs["lean_loogle"])
                        self.tool_call_counts["lean_loogle"] += 1
                        if not self._uses_local_loogle(tool_configs["lean_loogle"]):
                            await asyncio.sleep(15)

                    elif tool_call.name == "lean_run_code":
                        result_text, log_details = await execute_run_code_tool(tool_call, tool_configs["lean_run_code"])
                        self.tool_call_counts["lean_run_code"] += 1

                    elif tool_call.name == "submit_proof":
                        proof_text, log_details = extract_submission(tool_call)
                        self.tool_call_counts["submit_proof"] += 1

                        if "error" in log_details:
                            self._record_tool_usage(tool_usage_log, tool_call, log_details)
                            tool_results.append(ToolResult(tool_call=tool_call, result=proof_text))
                            continue

                        is_valid, verify_msg = await self._verify_submission(proof_text)
                        log_details["verified"] = is_valid
                        log_details["verify_message"] = verify_msg
                        self._record_tool_usage(tool_usage_log, tool_call, log_details)

                        return SimpleNamespace(
                            output_text=proof_text,
                            tool_calls=[],
                            history=result.history,
                            verified=is_valid,
                            verify_message=verify_msg,
                            stats=self.get_stats(),
                        ), tool_usage_log

                    else:
                        result_text = json.dumps({"error": f"Unsupported tool: {tool_call.name}"})
                        log_details = {"error": f"Unsupported tool: {tool_call.name}"}
                except Exception as exc:
                    logger.exception("Tool execution failed for %s", tool_call.name)
                    result_text = json.dumps({"error": f"Tool execution failed: {exc}"})
                    log_details = {
                        "error": f"Tool execution failed: {exc}",
                        "exception_type": type(exc).__name__,
                    }

                self._record_tool_usage(tool_usage_log, tool_call, log_details)
                tool_results.append(ToolResult(tool_call=tool_call, result=result_text))

            query_args["input"] = [*tool_results, TextInput(text=self._turn_status())]
            query_args["history"] = result.history

        return SimpleNamespace(
            output_text=getattr(result, "output_text", "") if result else "",
            tool_calls=getattr(result, "tool_calls", []) if result else [],
            history=getattr(result, "history", []) if result else [],
            verified=False,
            verify_message="No submission made",
            stats=self.get_stats(),
        ), tool_usage_log
