import asyncio
import contextvars
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import LLMAgent
from .prompts import build_prompt
from .tools import ToolConfig
from .utils import _strip_leading_empty_lines, _strip_response_and_format_proof, strip_comment_blocks

PROBLEM_LOG_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar("problem_log_context", default=None)
_ORIGINAL_LOG_RECORD_FACTORY = logging.getLogRecordFactory()


def _problem_log_record_factory(*args, **kwargs):
    record = _ORIGINAL_LOG_RECORD_FACTORY(*args, **kwargs)
    record.problem_log_owner = PROBLEM_LOG_CONTEXT.get()
    return record


logging.setLogRecordFactory(_problem_log_record_factory)


class ProofResult:
    """Result of a single proof attempt."""

    def __init__(
        self,
        attempt_num: int,
        is_valid: bool,
        model_response: str,
        processed_response: str,
        full_code: str | None = None,
        tool_logs: list[dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
    ):
        self.attempt_num = attempt_num
        self.is_valid = is_valid
        self.model_response = model_response
        self.processed_response = processed_response
        self.full_code = full_code
        self.tool_logs = tool_logs or []
        self.stats = stats or {}


class ProblemResult:
    """Result of proving attempts for a single problem."""

    def __init__(self, problem_id: str, attempts: list[ProofResult]):
        self.id = problem_id
        self.pass_at_k = any(attempt.is_valid for attempt in attempts)
        self.successful_attempts = sum(attempt.is_valid for attempt in attempts)
        self.total_attempts = len(attempts)
        self.max_attempts = self.total_attempts

        _, _, token_usage, total_cost = _aggregate_attempt_stats(attempts)
        self.token_usage = token_usage
        self.total_cost = total_cost
        self.total_input_tokens = token_usage.get("total_input_tokens", 0)
        self.total_output_tokens = token_usage.get("total_output_tokens", 0)

        self.attempts = [
            {
                "attempt_num": a.attempt_num,
                "is_valid": a.is_valid,
                "model_response": a.model_response,
                "processed_response": a.processed_response,
                "full_code": a.full_code,
                "tool_logs": a.tool_logs,
                "stats": a.stats,
            }
            for a in attempts
        ]


def _aggregate_attempt_stats(attempts: list["ProofResult"]) -> tuple[int, dict[str, int], dict[str, int], float]:
    """Aggregate tool usage, tokens, and cost across multiple attempts."""
    total_tool_calls = 0
    tool_breakdown: dict[str, int] = {}
    token_usage: dict[str, int] = {}
    total_cost = 0.0

    for attempt in attempts:
        stats = attempt.stats or {}
        total_tool_calls += stats.get("total_tool_calls", 0) or 0
        for tool_name, count in stats.get("tool_call_breakdown", {}).items():
            tool_breakdown[tool_name] = tool_breakdown.get(tool_name, 0) + count
        for token_key, count in stats.get("token_usage", {}).items():
            token_usage[token_key] = token_usage.get(token_key, 0) + count
        total_cost += stats.get("total_cost", 0) or 0

    return total_tool_calls, tool_breakdown, token_usage, total_cost


def aggregate_results(
    results: list[ProblemResult],
    proof_results: list[list["ProofResult"]] | None = None,
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
            "max_possible_attempts": 0,
            "successful_attempts": 0,
            "overall_success_rate": 0.0,
            "total_tool_calls": 0,
            "tool_call_breakdown": {},
            "token_usage": {},
            "total_cost": 0.0,
            "duration_seconds": 0.0,
        }

    total = len(results)
    pass_at_k_count = sum(r.pass_at_k for r in results)
    pass_at_k_rate = pass_at_k_count / total

    actual_attempts = sum(r.total_attempts for r in results)
    max_possible_attempts = sum(r.max_attempts for r in results)
    successful_attempts = sum(r.successful_attempts for r in results)
    overall_success_rate = successful_attempts / actual_attempts if actual_attempts > 0 else 0.0

    total_tool_calls = 0
    tool_breakdown: dict[str, int] = {}
    token_usage: dict[str, int] = {}
    total_cost = 0.0

    if proof_results:
        for problem_attempts in proof_results:
            problem_tool_calls, problem_tools, problem_tokens, problem_cost = _aggregate_attempt_stats(problem_attempts)
            total_tool_calls += problem_tool_calls
            for tool_name, count in problem_tools.items():
                tool_breakdown[tool_name] = tool_breakdown.get(tool_name, 0) + count
            for token_key, count in problem_tokens.items():
                token_usage[token_key] = token_usage.get(token_key, 0) + count
            total_cost += problem_cost

    duration_seconds = 0.0
    if start_time and end_time:
        duration_seconds = (end_time - start_time).total_seconds()

    return {
        "total_theorems": total,
        "pass_at_k_count": pass_at_k_count,
        "pass_at_k_rate": pass_at_k_rate,
        "actual_attempts": actual_attempts,
        "max_possible_attempts": max_possible_attempts,
        "successful_attempts": successful_attempts,
        "overall_success_rate": overall_success_rate,
        "total_tool_calls": total_tool_calls,
        "tool_call_breakdown": tool_breakdown,
        "token_usage": token_usage,
        "total_cost": total_cost,
        "duration_seconds": duration_seconds,
    }


def _setup_problem_logging(
    log_dir: Path | None, problem_id: str
) -> tuple[logging.Handler | None, contextvars.Token[str | None] | None]:
    """Set up per-problem logging if log_dir is provided."""
    if not log_dir:
        return None, None

    problem_log_dir = log_dir / problem_id
    problem_log_dir.mkdir(parents=True, exist_ok=True)

    problem_log_file = problem_log_dir / "full_trace.log"
    problem_log_handler = logging.FileHandler(problem_log_file)
    problem_log_handler.setLevel(logging.DEBUG)
    problem_log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    token = PROBLEM_LOG_CONTEXT.set(problem_id)

    class _ProblemLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - simple predicate
            return getattr(record, "problem_log_owner", None) == problem_id

    problem_log_handler.addFilter(_ProblemLogFilter())
    logging.getLogger().addHandler(problem_log_handler)

    return problem_log_handler, token


async def _run_single_attempt(
    item: dict[str, Any],
    attempt_num: int,
    model_str: str,
    include_nl_proof: bool,
    loogle_config: ToolConfig | None,
    run_code_config: ToolConfig | None,
    max_turns: int = 40,
) -> ProofResult:
    """Run a single proof attempt for a problem."""
    logging.info(f"  Attempt {attempt_num + 1}")

    system_prompt, user_prompt = build_prompt(
        item,
        include_nl_proof=include_nl_proof,
        use_tools=loogle_config is not None or run_code_config is not None,
        max_turns=max_turns,
    )

    logging.info(f"Full user prompt to model:\n{user_prompt}\n")
    logging.debug(f"System prompt (first 200 chars):\n{system_prompt[:200]}...\n")

    tool_usage_log: list[dict[str, Any]] = []

    try:
        agent = LLMAgent(
            model_str,
            loogle_config=loogle_config,
            run_code_config=run_code_config,
            enable_submission_tool=True,
            max_turns=max_turns,
            problem_context={"header": item.get("header", ""), "formal": item.get("formal", "")},
        )
        result, tool_usage_log = await agent.run(user_prompt, system_prompt=system_prompt)
        response = result.output_text if hasattr(result, "output_text") else str(result)
        logging.info(f"Raw model response (unprocessed):\n{response}\n")
        processed_response = _strip_response_and_format_proof(response)
        logging.info(f"Model response (processed):\n{processed_response}\n")

        if hasattr(result, "tool_calls") and result.tool_calls:
            logging.info(f"Tool calls made: {len(result.tool_calls)}")
            logging.info(f"Tool calls details:\n{json.dumps(result.tool_calls, indent=2, default=str)}")

    except Exception as e:
        logging.warning(f"LLM query failed on attempt {attempt_num + 1}: {e}")
        diagnostic = f"[internal_error] attempt {attempt_num + 1} failed: {type(e).__name__}: {e}"
        return ProofResult(
            attempt_num + 1,
            False,
            diagnostic,
            diagnostic,
            tool_logs=tool_usage_log,
            stats={"error": str(e), "error_type": type(e).__name__, "attempt_failed": True},
        )

    processing_error: str | None = None
    if processed_response is None:
        processing_error = "response rejected during post-processing"
        processed_response = ""

    if not processed_response.strip():
        logging.info("Skipping proof check - empty response from LLM")
        stats = dict(getattr(result, "stats", {}) or {})
        if processing_error:
            stats["processing_error"] = processing_error
        return ProofResult(attempt_num + 1, False, response, processed_response, tool_logs=tool_usage_log, stats=stats)

    clean_header = strip_comment_blocks(item["header"])
    clean_header = "\n".join(_strip_leading_empty_lines(clean_header.split("\n")))
    formal_code = item["formal"]
    if ":=" in formal_code:
        formal_code = formal_code.split(":=")[0].strip() + " :="
    full_code = f"{clean_header}\n{formal_code}\n{processed_response}"

    is_valid = getattr(result, "verified", False)
    logging.info(f"Proof valid (from agent verification): {is_valid}")

    stats = getattr(result, "stats", {})
    return ProofResult(
        attempt_num + 1,
        is_valid,
        response,
        processed_response,
        full_code,
        tool_logs=tool_usage_log,
        stats=stats,
    )


async def _process_single_problem(
    item: dict[str, Any],
    model_str: str,
    k: int,
    include_nl_proof: bool,
    loogle_config: ToolConfig | None,
    run_code_config: ToolConfig | None,
    log_dir: Path | None,
    max_turns: int = 40,
) -> tuple[ProblemResult, list[ProofResult]]:
    """Process all attempts for a single problem."""
    problem_id = item["id"]
    logging.info(f"Processing theorem {problem_id} with k={k} attempts")

    problem_log_handler, problem_log_token = _setup_problem_logging(log_dir, problem_id)

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
            )
            attempts.append(result)

            if result.is_valid:
                logging.info(f"  ✓ Success on attempt {attempt_num + 1}! Breaking early.")
                break

        problem_result = ProblemResult(problem_id, attempts)
        logging.info(
            f"Result for theorem {problem_id}: Pass@{k}={problem_result.pass_at_k} "
            f"({problem_result.successful_attempts}/{problem_result.total_attempts} successful)"
        )

        if log_dir:
            json_file = log_dir / problem_id / "results.json"

            total_tool_calls, tool_breakdown, token_usage, total_cost = _aggregate_attempt_stats(attempts)

            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "id": problem_result.id,
                        "success": problem_result.pass_at_k,
                        "total_tool_calls": total_tool_calls,
                        "tool_call_breakdown": tool_breakdown,
                        "token_usage": token_usage,
                        "total_cost": total_cost,
                        "successful_attempts": problem_result.successful_attempts,
                        "total_attempts": problem_result.total_attempts,
                        "max_attempts": k,
                        "attempts": [
                            {
                                "attempt_num": a.attempt_num,
                                "is_valid": a.is_valid,
                                "stats": a.stats,
                            }
                            for a in attempts
                        ],
                    },
                    f,
                    indent=2,
                )
            logging.info(f"Wrote results to {json_file}")

        return problem_result, attempts
    finally:
        try:
            from proof_bench.tools import cleanup_current_task_mcp_client

            await asyncio.wait_for(cleanup_current_task_mcp_client(), timeout=20)
        except TimeoutError:
            logging.warning("Timed out while cleaning up MCP clients")
        except Exception as e:
            logging.debug(f"MCP cleanup error (non-fatal): {e}")

        if problem_log_handler:
            logging.getLogger().removeHandler(problem_log_handler)
            problem_log_handler.close()
        if problem_log_token:
            PROBLEM_LOG_CONTEXT.reset(problem_log_token)


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
    from .tools import _loop_exception_handlers, _suppress_mcp_cleanup_errors

    loop = None
    start_time = datetime.now()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop_exception_handlers[loop] = loop.get_exception_handler()
        loop.set_exception_handler(_suppress_mcp_cleanup_errors)

        results = []
        all_proof_results: list[list[ProofResult]] = []
        for item in processed_dataset:
            problem_result, attempts = loop.run_until_complete(
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
            all_proof_results.append(attempts)

        end_time = datetime.now()
        summary = aggregate_results(results, all_proof_results, start_time, end_time)
        logging.info(f"Results Summary: {summary}")

        if log_dir:
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
