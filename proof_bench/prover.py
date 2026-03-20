import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from model_library.agent import AgentResult, TurnSummary

from .agent import run_agent
from .prompts import build_prompt
from .tools import ToolConfig
from .utils import _strip_leading_empty_lines, _strip_response_and_format_proof, strip_comment_blocks


class ProofResult:
    """Result of a single proof attempt."""

    def __init__(
        self,
        attempt_num: int,
        is_valid: bool,
        model_response: str,
        processed_response: str,
        full_code: str | None = None,
        agent_result: AgentResult | None = None,
    ):
        self.attempt_num = attempt_num
        self.is_valid = is_valid
        self.model_response = model_response
        self.processed_response = processed_response
        self.full_code = full_code
        self.agent_result = agent_result


class ProblemResult:
    """Result of proving attempts for a single problem."""

    def __init__(self, problem_id: str, attempts: list[ProofResult]):
        self.id = problem_id
        self.pass_at_k = any(a.is_valid for a in attempts)
        self.successful_attempts = sum(a.is_valid for a in attempts)
        self.total_attempts = len(attempts)

        self.agent_results = [a.agent_result for a in attempts if a.agent_result is not None]
        self.attempts = [
            {
                "attempt_num": a.attempt_num,
                "is_valid": a.is_valid,
                "model_response": a.model_response,
                "processed_response": a.processed_response,
                "full_code": a.full_code,
            }
            for a in attempts
        ]


def aggregate_results(
    results: list[ProblemResult],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    """Compute summary statistics from proving results."""
    if not results:
        return {
            "total_theorems": 0,
            "pass_at_k_count": 0,
            "pass_at_k_rate": 0.0,
            "actual_attempts": 0,
            "successful_attempts": 0,
            "overall_success_rate": 0.0,
            "total_tool_calls": 0,
            "tool_call_breakdown": {},
            "total_cost": 0.0,
            "duration_seconds": 0.0,
        }

    total = len(results)
    pass_at_k_count = sum(r.pass_at_k for r in results)
    actual_attempts = sum(r.total_attempts for r in results)
    successful_attempts = sum(r.successful_attempts for r in results)

    total_tool_calls = 0
    tool_breakdown: dict[str, int] = {}
    total_cost = 0.0

    for r in results:
        for ar in r.agent_results:
            total_tool_calls += ar.tool_calls_count
            for name, count in ar.tool_usage.items():
                tool_breakdown[name] = tool_breakdown.get(name, 0) + count
            meta = ar.final_aggregated_metadata
            total_cost += meta.cost.total if meta.cost else 0.0

    duration_seconds = 0.0
    if start_time and end_time:
        duration_seconds = (end_time - start_time).total_seconds()

    return {
        "total_theorems": total,
        "pass_at_k_count": pass_at_k_count,
        "pass_at_k_rate": pass_at_k_count / total,
        "actual_attempts": actual_attempts,
        "successful_attempts": successful_attempts,
        "overall_success_rate": successful_attempts / actual_attempts if actual_attempts > 0 else 0.0,
        "total_tool_calls": total_tool_calls,
        "tool_call_breakdown": tool_breakdown,
        "total_cost": total_cost,
        "duration_seconds": duration_seconds,
    }


async def _run_single_attempt(
    item: dict[str, Any],
    attempt_num: int,
    model_str: str,
    include_nl_proof: bool,
    loogle_config: ToolConfig | None,
    run_code_config: ToolConfig | None,
    max_turns: int = 40,
    log_dir: Path | None = None,
) -> ProofResult:
    """Run a single proof attempt for a problem."""
    system_prompt, user_prompt = build_prompt(
        item,
        include_nl_proof=include_nl_proof,
        use_tools=loogle_config is not None or run_code_config is not None,
        max_turns=max_turns,
    )

    try:
        result = await run_agent(
            model_str,
            user_prompt,
            system_prompt=system_prompt,
            loogle_config=loogle_config,
            run_code_config=run_code_config,
            problem_context={"header": item.get("header", ""), "formal": item.get("formal", "")},
            max_turns=max_turns,
            question_id=item.get("id", "proof"),
            log_dir=log_dir or Path("logs"),
        )
    except Exception as e:
        logger.warning("LLM query failed on attempt %d: %s", attempt_num + 1, e)
        diagnostic = f"[internal_error] attempt {attempt_num + 1} failed: {type(e).__name__}: {e}"
        return ProofResult(attempt_num + 1, False, diagnostic, diagnostic)

    response = result.final_answer
    processed_response = _strip_response_and_format_proof(response)
    if processed_response is None:
        processed_response = ""

    if not processed_response.strip():
        return ProofResult(attempt_num + 1, False, response, processed_response, agent_result=result)

    clean_header = strip_comment_blocks(item["header"])
    clean_header = "\n".join(_strip_leading_empty_lines(clean_header.split("\n")))
    formal_code = item["formal"]
    if ":=" in formal_code:
        formal_code = formal_code.split(":=")[0].strip() + " :="
    full_code = f"{clean_header}\n{formal_code}\n{processed_response}"

    is_valid = any(
        tc.success
        for turn in result.turns
        if isinstance(turn, TurnSummary)
        for tc in turn.tool_calls
        if tc.tool_name == "submit_proof" and tc.done
    )
    return ProofResult(attempt_num + 1, is_valid, response, processed_response, full_code, agent_result=result)


async def _process_single_problem(
    item: dict[str, Any],
    model_str: str,
    k: int,
    include_nl_proof: bool,
    loogle_config: ToolConfig | None,
    run_code_config: ToolConfig | None,
    log_dir: Path | None,
    max_turns: int = 40,
) -> ProblemResult:
    """Process all attempts for a single problem."""
    try:
        attempts = []
        for attempt_num in range(k):
            result = await _run_single_attempt(
                item,
                attempt_num,
                model_str,
                include_nl_proof,
                loogle_config,
                run_code_config,
                max_turns,
                log_dir,
            )
            attempts.append(result)
            if result.is_valid:
                break

        return ProblemResult(item["id"], attempts)
    finally:
        try:
            from proof_bench.mcp_client import cleanup_current_task_mcp_client

            await asyncio.wait_for(cleanup_current_task_mcp_client(), timeout=20)
        except (TimeoutError, Exception):
            pass


def run_proving_pipeline(
    processed_dataset: list[dict[str, Any]],
    model_str: str,
    k: int = 8,
    include_nl_proof: bool = False,
    loogle_config: ToolConfig | None = None,
    run_code_config: ToolConfig | None = None,
    log_dir: Path | None = None,
    max_turns: int = 40,
) -> tuple[list[ProblemResult], dict[str, Any]]:
    """Run the proving pipeline on a dataset."""
    from .mcp_client import _loop_exception_handlers, _suppress_mcp_cleanup_errors

    loop = None
    start_time = datetime.now()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop_exception_handlers[loop] = loop.get_exception_handler()
        loop.set_exception_handler(_suppress_mcp_cleanup_errors)

        results = []
        for item in processed_dataset:
            problem_result = loop.run_until_complete(
                _process_single_problem(
                    item,
                    model_str,
                    k,
                    include_nl_proof,
                    loogle_config,
                    run_code_config,
                    log_dir,
                    max_turns,
                )
            )
            results.append(problem_result)

        end_time = datetime.now()
        summary = aggregate_results(results, start_time, end_time)

        if log_dir:
            import json

            summary_file = log_dir / "aggregated_results.json"
            with open(summary_file, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)

        return results, summary
    finally:
        if loop:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            finally:
                loop.close()
                asyncio.set_event_loop(None)
