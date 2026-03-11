#!/usr/bin/env python3
"""Validates Lean file headers and exports metadata to JSONL.

Only processes files in problems/ directory (excludes negated/).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from proof_bench.metadata_utils import check_no_sorry, export_jsonl, validate_headers


def validate_and_report(problems_dir: Path) -> bool:
    """Validate Lean files and report results. Returns True if validation passes."""
    print(f"Validating Lean file headers in {problems_dir}...")
    bad_headers = validate_headers(problems_dir)
    if bad_headers:
        print("ERROR: Missing metadata in:")
        for f in bad_headers:
            print(f"  {f}")
        return False
    print("✓ All Lean files have required metadata.")
    return True


def check_sorry_and_report(problems_dir: Path) -> None:
    """Check for sorry statements and report results."""
    print("\nChecking for sorry statements...")
    files_with_sorry = check_no_sorry(problems_dir)
    if files_with_sorry:
        print("WARNING: Files contain 'sorry':")
        for f in files_with_sorry:
            print(f"  {f}")
    else:
        print("✓ No sorry statements found.")


def export_metadata(base_dir: Path, problems_dir: Path, output_file: Path) -> None:
    """Export metadata to JSONL file."""
    print(f"\nExporting metadata from {problems_dir} to {output_file}...")
    try:
        export_jsonl(problems_dir, output_file, base_dir=base_dir)
        print(f"✓ Metadata exported successfully to {output_file}")
    except Exception as e:
        print(f"ERROR: Failed to export metadata: {e}")
        sys.exit(1)


def main() -> None:
    """Validate Lean files and export metadata from problems/ only."""
    base_dir = Path(__file__).parent.parent
    problems_dir = base_dir / "problems"
    output_file = base_dir / "data" / "proof-bench.jsonl"

    if not problems_dir.exists():
        print(f"ERROR: problems directory not found: {problems_dir}")
        sys.exit(1)

    output_file.parent.mkdir(parents=True, exist_ok=True)

    if not validate_and_report(problems_dir):
        sys.exit(1)

    check_sorry_and_report(problems_dir)
    export_metadata(base_dir, problems_dir, output_file)


if __name__ == "__main__":
    main()
