import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from proof_bench.load_problems import load_exported_problems
from proof_bench.paths import runtime_data_dir
from proof_bench.prover import run_proving_pipeline
from proof_bench.tools import ToolConfig

DEFAULT_LOOGLE_TRANSPORT = "stdio"
DEFAULT_LOOGLE_MAX_RESULTS = 8
VALID_DATASETS = {"exported"}


def _resolve_stdio_command() -> list[str]:
    if uvx := shutil.which("uvx"):
        return [uvx, "lean-lsp-mcp", "--transport", "stdio"]
    if python3 := shutil.which("python3"):
        return [python3, "-m", "lean_lsp_mcp", "--transport", "stdio"]
    if python := shutil.which("python"):
        return [python, "-m", "lean_lsp_mcp", "--transport", "stdio"]
    raise RuntimeError("Could not find uvx/python3/python to launch lean-lsp-mcp")


def validate_args(args: argparse.Namespace) -> None:
    """Validate command line arguments."""
    if not args.model:
        logging.error("Model must be specified")
        sys.exit(1)
    if args.k <= 0:
        logging.error("k must be positive")
        sys.exit(1)
    if not (0 <= args.temperature <= 2):
        logging.error("Temperature must be between 0 and 2")
        sys.exit(1)
    if args.problem_id and getattr(args, "domains", None):
        logging.error("Cannot specify both --problem-id and --domains")
        sys.exit(1)


def setup_logging(args: argparse.Namespace) -> tuple[str, Path]:
    """Configure logging to file and console."""
    validate_args(args)

    base_log_dir = runtime_data_dir() / "logs"
    base_log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = base_log_dir / timestamp
    log_dir.mkdir(parents=True, exist_ok=True)

    if args.log_file:
        log_filename = str(log_dir / args.log_file)
    else:
        model_name = args.model.replace("/", "_")
        log_filename = str(log_dir / f"proof_bench_{model_name}_k{args.k}_temp{args.temperature}.log")

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_filename), logging.StreamHandler()],
        force=True,
    )

    return log_filename, log_dir


def load_dataset(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Load the appropriate dataset based on arguments."""
    if args.dataset not in VALID_DATASETS:
        logging.error(f"Invalid dataset: {args.dataset}. Must be 'exported'")
        sys.exit(1)

    logger = logging.getLogger(__name__)

    try:
        dataset = load_exported_problems()
        logger.info(f"Loaded {len(dataset)} problems from exported Lean files")
    except Exception as e:
        logging.error(f"Failed to load dataset: {e}")
        sys.exit(1)

    return dataset


def filter_dataset(
    dataset: list[dict[str, Any]], problem_id: str | None, domains: list[str] | None
) -> tuple[list[dict[str, Any]], str]:
    """Filter dataset by problem ID or domain substrings."""
    if not problem_id and not domains:
        return dataset, "all"

    if problem_id:
        filtered_dataset = [item for item in dataset if item["id"] == problem_id]
        if not filtered_dataset:
            logging.error(f"Problem with ID '{problem_id}' not found in dataset")
            return [], problem_id
        logging.info(f"Running single problem: {problem_id}")
        return filtered_dataset, problem_id

    if domains:

        def matches_domain(item: dict[str, Any]) -> bool:
            item_id = item["id"].lower()
            return any(domain.lower() in item_id for domain in domains)

        filtered_dataset = [item for item in dataset if matches_domain(item)]
        domain_str = ", ".join(domains)
        if not filtered_dataset:
            logging.error(f"No problems found matching domains: {domain_str}")
            return [], f"domains:{domain_str}"

        logging.info(f"Running {len(filtered_dataset)} problems matching domains: {domain_str}")
        return filtered_dataset, f"domains:{domain_str}"

    return dataset, "all"


def write_metadata(args: argparse.Namespace, log_dir: Path, problem_scope: str, dataset_size: int) -> None:
    """Write run metadata to JSON file."""
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "dataset": args.dataset,
        "model": args.model,
        "k": args.k,
        "temperature": args.temperature,
        "include_nl_proof": args.include_nl_proof,
        "loogle_enabled": args.enable_loogle,
        "loogle_transport": "stdio" if args.enable_loogle else None,
        "loogle_max_results": DEFAULT_LOOGLE_MAX_RESULTS if args.enable_loogle else None,
        "problem_scope": problem_scope,
        "domains": getattr(args, "domains", None),
        "total_problems": dataset_size,
    }

    metadata_file = log_dir / "metadata.json"
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    logging.info(f"Metadata saved to: {metadata_file}")


def build_tool_configs(args: argparse.Namespace) -> tuple[ToolConfig | None, ToolConfig]:
    base_config: ToolConfig = {
        "transport": DEFAULT_LOOGLE_TRANSPORT,
        "project_path": str(Path(__file__).parent),
        "stdio_command": _resolve_stdio_command(),
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
    parser = argparse.ArgumentParser(description="Proof Bench: Automated Theorem Proving Benchmark")
    parser.add_argument("--dataset", type=str, default="exported", choices=["exported"])
    parser.add_argument(
        "--model",
        type=str,
        default="openai/gpt-4o",
        help="Model name in format 'provider/model-name' (e.g., 'openai/gpt-4o', 'google/gemini-2.5-flash', 'grok/grok-4-0709')",
    )
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--log-file", type=str)
    parser.add_argument("--problem-id", type=str, help="Run only the problem with this specific ID")
    parser.add_argument(
        "--domains",
        nargs="+",
        help="Run only problems whose IDs contain any of these substrings (e.g., logic number_theory measure)",
    )
    parser.add_argument(
        "--include-nl-proof",
        action="store_true",
        default=False,
        help="Include natural language proof in the prompt as a hint",
    )
    parser.add_argument(
        "--enable-loogle",
        action="store_true",
        default=False,
        help="Enable lean-lsp-mcp's Loogle MCP tool for theorem search",
    )
    parser.add_argument(
        "--loogle-local",
        action="store_true",
        default=False,
        help="Use local Loogle server (bypasses 3 queries/30s rate limit)",
    )

    args = parser.parse_args()

    log_filename, log_dir = setup_logging(args)
    logger = logging.getLogger(__name__)
    logger.info(f"Proof Bench started: dataset={args.dataset}, model={args.model}, k={args.k}, temp={args.temperature}")
    logger.info(f"Log file: {log_filename}")

    dataset = load_dataset(args)
    if not dataset:
        logging.error("No problems loaded from dataset")
        return

    dataset, problem_scope = filter_dataset(dataset, args.problem_id, getattr(args, "domains", None))
    if not dataset:
        return

    num_problems = len(dataset)
    logger.info(f"Ready to evaluate {num_problems} problem{'s' if num_problems != 1 else ''}")
    if input("Proceed? (Y/n): ").lower().strip() not in ("", "y", "yes"):
        logger.info("Evaluation cancelled by user")
        return

    write_metadata(args, log_dir, problem_scope, num_problems)

    loogle_config, run_code_config = build_tool_configs(args)

    try:
        run_proving_pipeline(
            dataset,
            args.model,
            k=args.k,
            include_nl_proof=args.include_nl_proof,
            loogle_config=loogle_config,
            run_code_config=run_code_config,
            log_dir=log_dir,
        )
    except Exception as e:
        logging.error(f"Proving pipeline failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
