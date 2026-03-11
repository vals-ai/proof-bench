"""Load and parse problem files for the proving pipeline."""

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_THEOREM_PATTERN = re.compile(r"(theorem\s+\w+.*?:=)", re.DOTALL)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXPORTED_FILE = Path("data") / "proof-bench.jsonl"
_STANDALONE_ROOT = Path("/home/ec2-user/proof-bench")


def _build_problem_entry(
    *,
    problem_id: str,
    header: str,
    formal: str,
    natural: str,
    nl_proof: str,
    formal_proof: str = "",
) -> dict[str, str]:
    return {
        "id": problem_id,
        "header": header,
        "formal": formal,
        "natural": natural,
        "nl_proof": nl_proof,
        "formal_proof": formal_proof,
    }


def _resolve_exported_paths() -> tuple[Path, Path]:
    output_file = _REPO_ROOT / _EXPORTED_FILE
    if output_file.exists():
        return output_file, _REPO_ROOT

    standalone_file = _STANDALONE_ROOT / _EXPORTED_FILE
    if standalone_file.exists():
        return standalone_file, _STANDALONE_ROOT

    raise FileNotFoundError(f"Data file not found: {output_file} or {standalone_file}")


def load_exported_problems() -> list[dict[str, Any]]:
    """Load problems from exported Proof Bench JSONL metadata and Lean files."""
    output_file, base_dir = _resolve_exported_paths()

    problems: list[dict[str, str]] = []
    try:
        with open(output_file, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    metadata = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping invalid JSON at line {line_num}: {e}")
                    continue

                lean_file = base_dir / "problems" / metadata.get("file", "")
                if not lean_file.exists():
                    logger.warning(f"Lean file not found: {lean_file}")
                    continue

                try:
                    content = lean_file.read_text(encoding="utf-8")
                except OSError as e:
                    logger.warning(f"Failed to read Lean file {lean_file}: {e}")
                    continue

                if not _THEOREM_PATTERN.search(content):
                    logger.warning(f"No theorem found in {lean_file}")
                    continue

                problems.append(
                    _build_problem_entry(
                        problem_id=metadata.get("id", ""),
                        header=metadata.get("header", ""),
                        formal=metadata.get("formal", ""),
                        natural=metadata.get("statement", ""),
                        nl_proof=metadata.get("nl_proof", ""),
                    )
                )
    except OSError as e:
        raise RuntimeError(f"Failed to read data file {output_file}: {e}") from e

    logger.info(f"Loaded {len(problems)} problems from exported Lean files")
    return problems


def load_exported_alias(
    alias: str,
    problems: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the exported dataset while logging which alias requested it."""
    exported = problems if problems is not None else load_exported_problems()
    logger.info(
        "Dataset alias '%s' mapped to exported corpus containing %d problems",
        alias,
        len(exported),
    )
    return exported
