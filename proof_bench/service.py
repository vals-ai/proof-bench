"""Service wrapper for Proof Bench that provides a simple interface for solving individual problems."""

import logging
from pathlib import Path
from typing import Any

from .load_problems import (
    load_exported_alias,
    load_exported_problems,
)

logger = logging.getLogger(__name__)


class ProofBenchService:
    """Service for solving Proof Bench problems."""

    def __init__(self):
        """Initialize the service."""
        self._exported_problems: list[dict[str, Any]] | None = None
        self._exported_problem_index: dict[str, dict[str, Any]] = {}
        self._exported_aliases: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _is_exported_dataset(dataset: str) -> bool:
        return dataset in ("exported", "proof_bench", "atp_bench") or dataset.startswith("exported_")

    def _get_exported_dataset(self, alias: str) -> list[dict[str, Any]]:
        if alias == "exported":
            if self._exported_problems is None:
                self._exported_problems = load_exported_problems()
                self._exported_problem_index = {p["id"]: p for p in self._exported_problems}
            return self._exported_problems

        if alias not in self._exported_aliases:
            if self._exported_problems is None:
                self._exported_problems = load_exported_problems()
            self._exported_aliases[alias] = load_exported_alias(alias, self._exported_problems)
        return self._exported_aliases[alias]

    def _load_dataset(self, dataset: str) -> list[dict[str, Any]]:
        if self._is_exported_dataset(dataset):
            return self._get_exported_dataset(dataset)

        raise ValueError(f"Unknown dataset: {dataset}")

    def _get_problem(self, problem_id: str, dataset: str) -> dict | None:
        """Load and return a problem by ID from the specified dataset."""
        if dataset == "exported":
            self._get_exported_dataset("exported")  # ensure loaded
            return self._exported_problem_index.get(problem_id)

        problems = self._load_dataset(dataset)
        return next((p for p in problems if p["id"] == problem_id), None)

    async def solve_problem(
        self,
        problem_id: str,
        dataset: str = "exported",
        model: str = "openai/gpt-4o-mini-2024-07-18",
        k: int = 4,
        include_nl_proof: bool = False,
        log_dir: Path | str | None = None,
        loogle_config: dict | None = None,
        run_code_config: dict | None = None,
        max_turns: int = 40,
    ):
        """Solve a single problem and return the result."""
        from proof_bench.prover import _process_single_problem

        problem = self._get_problem(problem_id, dataset)
        if not problem:
            raise ValueError(f"Problem '{problem_id}' not found in dataset '{dataset}'")
        if isinstance(log_dir, str):
            log_dir = Path(log_dir).expanduser()

        return await _process_single_problem(
            problem,
            model,
            k,
            include_nl_proof,
            loogle_config,
            run_code_config,
            log_dir,
            max_turns,
        )
