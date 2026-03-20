"""Corpus metadata and logs: outside the submodule when embedded under proof_bench/src/."""

from __future__ import annotations

from pathlib import Path

_HARNESS_ROOT = Path(__file__).resolve().parent.parent


def harness_root() -> Path:
    """Checkout root (contains problems/ and proof_bench/)."""
    return _HARNESS_ROOT


def runtime_data_dir() -> Path:
    """Directory for proof-bench.jsonl and data/logs/."""
    root = _HARNESS_ROOT
    if root.name == "public_harness" and root.parent.name == "src" and root.parent.parent.name == "proof_bench":
        return (root.parent.parent / "data").resolve()
    return (root / "data").resolve()
