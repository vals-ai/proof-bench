"""Custom model wrapper for Proof Bench platform integration."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from vals.sdk.types import OutputObject

from proof_bench.service import ProofBenchService

logger = logging.getLogger(__name__)

_service: ProofBenchService | None = None
_PROOF_BENCH_ROOT = Path(__file__).parent.parent
_BASE_LOG_DIR = _PROOF_BENCH_ROOT / "data" / "logs" / "benchmark_runs"
_LOOGLE_DEFAULT_WARNING_EMITTED = False

_DEFAULTS = {
    "dataset": "proof_bench",
    "k": 1,
    "include_nl_proof": False,
    "max_turns": 40,
}


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_stdio_command() -> list[str]:
    if uvx := shutil.which("uvx"):
        return [uvx, "lean-lsp-mcp", "--transport", "stdio"]
    if python3 := shutil.which("python3"):
        return [python3, "-m", "lean_lsp_mcp", "--transport", "stdio"]
    if python := shutil.which("python"):
        return [python, "-m", "lean_lsp_mcp", "--transport", "stdio"]
    raise RuntimeError("Could not find uvx/python3/python to launch lean-lsp-mcp")


def _make_tool_config() -> dict:
    global _LOOGLE_DEFAULT_WARNING_EMITTED

    config: dict[str, Any] = {
        "transport": "stdio",
        "project_path": str(_PROOF_BENCH_ROOT),
        "stdio_command": _resolve_stdio_command(),
    }
    if daemon_url := os.getenv("LOOGLE_DAEMON_URL"):
        config["loogle_daemon_url"] = daemon_url
    elif _env_enabled("LEAN_LOOGLE_LOCAL"):
        config["loogle_local"] = True
    else:
        config["loogle_local"] = False
        if not _LOOGLE_DEFAULT_WARNING_EMITTED:
            logger.warning(
                "LOOGLE_DAEMON_URL not set; defaulting to remote loogle mode. "
                "Set LOOGLE_DAEMON_URL for shared daemon, or LEAN_LOOGLE_LOCAL=true for local mode."
            )
            _LOOGLE_DEFAULT_WARNING_EMITTED = True
    return config


def _get_service() -> ProofBenchService:
    global _service
    if _service is None:
        _service = ProofBenchService()
    return _service


def _get_log_dir(model: str) -> Path:
    date_str = datetime.now().strftime("%Y%m%d")
    model_str = model.replace("/", "_").replace(":", "_")
    log_dir = _BASE_LOG_DIR / f"{date_str}_{model_str}"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _parse_test_input(raw: str, fallback_model: str) -> dict[str, Any]:
    stripped = raw.strip()
    if not stripped:
        raise ValueError("test_input is empty")

    try:
        parsed = json.loads(stripped)
        payload = {"problem_id": parsed} if isinstance(parsed, str) else parsed
        if not isinstance(payload, dict):
            raise ValueError("test_input must be a string or JSON object")
    except json.JSONDecodeError:
        payload = {"problem_id": stripped}

    merged = {**_DEFAULTS, **payload}
    merged["problem_id"] = str(merged.get("problem_id", "")).strip()
    if not merged["problem_id"]:
        raise ValueError("problem_id is required")

    merged["k"] = int(merged.get("k", _DEFAULTS["k"]))
    merged["include_nl_proof"] = bool(merged.get("include_nl_proof", False))
    merged["model"] = merged.get("model", fallback_model)
    merged["dataset"] = merged.get("dataset", _DEFAULTS["dataset"])
    merged["log_dir"] = merged.get("log_dir")
    merged["loogle_config"] = merged.get("loogle_config") or _make_tool_config()
    merged["run_code_config"] = merged.get("run_code_config") or _make_tool_config()
    merged["max_turns"] = int(merged.get("max_turns") or _DEFAULTS["max_turns"])

    if isinstance(merged["log_dir"], str) and merged["log_dir"].strip():
        merged["log_dir"] = Path(merged["log_dir"]).expanduser()

    return merged


async def get_custom_model(model_name: str, parameters: dict, *args, **kwargs):
    """Factory function that returns a custom_call coroutine for the platform."""
    _ = (parameters, args, kwargs)

    async def custom_call(test_input: str):
        config = _parse_test_input(test_input, model_name)
        service = _get_service()
        log_dir = config.get("log_dir") or _get_log_dir(config["model"])
        if isinstance(log_dir, str):
            log_dir = Path(log_dir).expanduser()

        start = time.perf_counter()
        try:
            result = await service.solve_problem(
                problem_id=config["problem_id"],
                dataset=config["dataset"],
                model=config["model"],
                k=config["k"],
                include_nl_proof=config["include_nl_proof"],
                log_dir=log_dir,
                loogle_config=config.get("loogle_config"),
                run_code_config=config.get("run_code_config"),
                max_turns=config["max_turns"],
            )
        except Exception as exc:
            logger.exception("Proof Bench run failed: %s", exc)
            return {"llm_output": "ERROR", "output_context": {"error": str(exc)}}

        duration = time.perf_counter() - start
        return OutputObject(
            llm_output="true" if result.pass_at_k else "false",
            in_tokens=result.total_input_tokens,
            out_tokens=result.total_output_tokens,
            duration=duration,
            cost=result.total_cost,
            output_context={
                "problem_id": result.id,
                "pass_at_k": result.pass_at_k,
                "attempts": result.attempts,
                "successful_attempts": result.successful_attempts,
                "total_attempts": result.total_attempts,
                "query_metadata": {
                    "cost": {"total": result.total_cost},
                    **result.token_usage,
                },
                "config": {
                    "dataset": config["dataset"],
                    "include_nl_proof": config["include_nl_proof"],
                    "max_turns": config["max_turns"],
                    "k": config["k"],
                    "model": config["model"],
                },
            },
        )

    return custom_call
