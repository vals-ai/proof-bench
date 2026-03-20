import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from proof_bench.load_problems import load_exported_problems
from proof_bench.mcp_client import resolve_stdio_command
from proof_bench.prover import ProblemResult, run_proving_pipeline
from proof_bench.tools import ToolConfig

DEFAULT_LOOGLE_TRANSPORT = "stdio"
DEFAULT_LOOGLE_MAX_RESULTS = 8
VALID_DATASETS = {"exported"}
BASE_LOG_DIR = Path(__file__).parent / "data" / "logs"


def validate_args(args: argparse.Namespace) -> None:
    if not args.model:
        print("error: model must be specified", file=sys.stderr)
        sys.exit(1)
    if args.k <= 0:
        print("error: k must be positive", file=sys.stderr)
        sys.exit(1)
    if not (0 <= args.temperature <= 2):
        print("error: temperature must be between 0 and 2", file=sys.stderr)
        sys.exit(1)
    if args.problem_id and getattr(args, "domains", None):
        print("error: cannot specify both --problem-id and --domains", file=sys.stderr)
        sys.exit(1)


def load_dataset(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.dataset not in VALID_DATASETS:
        print(f"error: invalid dataset '{args.dataset}'. Must be 'exported'", file=sys.stderr)
        sys.exit(1)
    try:
        dataset = load_exported_problems()
        print(f"Loaded {len(dataset)} problems")
        return dataset
    except Exception as e:
        print(f"error: failed to load dataset: {e}", file=sys.stderr)
        sys.exit(1)


def filter_dataset(
    dataset: list[dict[str, Any]], problem_id: str | None, domains: list[str] | None
) -> tuple[list[dict[str, Any]], str]:
    if not problem_id and not domains:
        return dataset, "all"

    if problem_id:
        filtered = [item for item in dataset if item["id"] == problem_id]
        if not filtered:
            print(f"error: problem '{problem_id}' not found", file=sys.stderr)
            return [], problem_id
        return filtered, problem_id

    if domains:
        def matches_domain(item: dict[str, Any]) -> bool:
            return any(domain.lower() in item["id"].lower() for domain in domains)

        filtered = [item for item in dataset if matches_domain(item)]
        domain_str = ", ".join(domains)
        if not filtered:
            print(f"error: no problems matching domains: {domain_str}", file=sys.stderr)
            return [], f"domains:{domain_str}"
        print(f"Filtered to {len(filtered)} problems matching: {domain_str}")
        return filtered, f"domains:{domain_str}"

    return dataset, "all"


def _resolve_run_dir(results: list[ProblemResult]) -> Path:
    """Return the agent's run directory, falling back to BASE_LOG_DIR."""
    for r in results:
        if r.agent_results:
            return r.agent_results[0].output_dir.parent
    return BASE_LOG_DIR


def write_results(
    args: argparse.Namespace,
    run_dir: Path,
    problem_scope: str,
    dataset_size: int,
    summary: dict[str, Any],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "dataset": args.dataset,
        "model": args.model,
        "k": args.k,
        "temperature": args.temperature,
        "include_nl_proof": args.include_nl_proof,
        "loogle_enabled": args.enable_loogle,
        "problem_scope": problem_scope,
        "domains": getattr(args, "domains", None),
        "total_problems": dataset_size,
    }
    with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    with open(run_dir / "aggregated_results.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)


def build_tool_configs(args: argparse.Namespace) -> tuple[ToolConfig | None, ToolConfig]:
    base_config: ToolConfig = {
        "transport": DEFAULT_LOOGLE_TRANSPORT,
        "project_path": str(Path(__file__).parent),
        "stdio_command": resolve_stdio_command(),
    }

    run_code_config: ToolConfig = dict(base_config)

    loogle_config: ToolConfig | None = None
    if args.enable_loogle:
        loogle_config = {
            **base_config,
            "max_results": DEFAULT_LOOGLE_MAX_RESULTS,
            "loogle_local": args.loogle_local,
        }

    return loogle_config, run_code_config


def main() -> None:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Proof Bench: Automated Theorem Proving Benchmark")
    parser.add_argument("--dataset", type=str, default="exported", choices=["exported"])
    parser.add_argument(
        "--model",
        type=str,
        default="openai/gpt-4o",
        help="Model name in format 'provider/model-name' (e.g., 'openai/gpt-4o', 'google/gemini-2.5-flash')",
    )
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--problem-id", type=str, help="Run only the problem with this specific ID")
    parser.add_argument(
        "--domains",
        nargs="+",
        help="Run only problems whose IDs contain any of these substrings",
    )
    parser.add_argument("--include-nl-proof", action="store_true", default=False)
    parser.add_argument(
        "--no-loogle",
        dest="enable_loogle",
        action="store_false",
        help="Disable the Loogle lemma search tool",
    )
    parser.add_argument(
        "--enable-loogle",
        dest="enable_loogle",
        action="store_true",
        help="Enable Loogle (default: on)",
    )
    parser.set_defaults(enable_loogle=True)
    parser.add_argument("--loogle-local", action="store_true", default=False)

    args = parser.parse_args()
    validate_args(args)

    dataset = load_dataset(args)
    if not dataset:
        print("error: no problems loaded", file=sys.stderr)
        return

    dataset, problem_scope = filter_dataset(dataset, args.problem_id, getattr(args, "domains", None))
    if not dataset:
        return

    num_problems = len(dataset)
    print(f"Ready to evaluate {num_problems} problem{'s' if num_problems != 1 else ''} with {args.model} (k={args.k})")
    if input("Proceed? (Y/n): ").lower().strip() not in ("", "y", "yes"):
        return

    loogle_config, run_code_config = build_tool_configs(args)

    try:
        results, summary = run_proving_pipeline(
            dataset,
            args.model,
            k=args.k,
            include_nl_proof=args.include_nl_proof,
            loogle_config=loogle_config,
            run_code_config=run_code_config,
            log_dir=BASE_LOG_DIR,
        )
    except Exception as e:
        print(f"error: proving pipeline failed: {e}", file=sys.stderr)
        sys.exit(1)

    run_dir = _resolve_run_dir(results)
    write_results(args, run_dir, problem_scope, num_problems, summary)
    print(f"Logs: {run_dir}")


if __name__ == "__main__":
    main()
