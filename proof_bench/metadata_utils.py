import json
import re
from pathlib import Path

from .lean_parser import find_lean_files, parse_lean_file
from .utils import _strip_leading_empty_lines, strip_comment_blocks


def validate_headers(base_dir: str | Path) -> list[Path]:
    """Ensure every Lean file has all required metadata fields."""
    base = Path(base_dir)
    invalid_files = []

    for file_path in find_lean_files(base):
        result = parse_lean_file(file_path, relative_to=base)
        if result["metadata"] is None:
            invalid_files.append(file_path)

    return invalid_files


def _extract_theorem_info(content: str) -> tuple[str, str]:
    """Extract theorem header and formal statement from Lean content."""
    cleaned_content = strip_comment_blocks(content)
    cleaned_content = "\n".join(_strip_leading_empty_lines(cleaned_content.split("\n")))

    theorem_match = re.search(r"(theorem\s+\w+)", cleaned_content)
    header = cleaned_content[: theorem_match.start()].strip() if theorem_match else "import Mathlib"

    formal_match = re.search(r"(theorem\s+\w+.*?:=)", cleaned_content, re.DOTALL)
    formal = formal_match.group(1).strip() if formal_match else ""

    return header, formal


def _read_informal_file(base_dir: Path, problem_name: str, suffix: str) -> str:
    """Read an informal .tex file for a problem, returning empty string when unavailable."""
    informal_dir = base_dir / "problems" / "informal"
    if not informal_dir.exists():
        return ""

    target_file = informal_dir / f"{problem_name}_{suffix}.tex"
    if not target_file.exists():
        return ""

    try:
        return target_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _find_statement(base_dir: Path, problem_name: str) -> str:
    """Find natural language statement for a problem."""
    return _read_informal_file(base_dir, problem_name, "statement")


def _find_nl_proof(base_dir: Path, problem_name: str) -> str:
    """Find natural language proof for a problem."""
    return _read_informal_file(base_dir, problem_name, "proof")


def export_jsonl(
    search_dir: str | Path,
    output_file: str | Path,
    base_dir: str | Path | None = None,
) -> None:
    """Export all Lean metadata to JSONL."""
    search = Path(search_dir)
    base = Path(base_dir) if base_dir else search
    files = find_lean_files(search)

    with open(output_file, "w", encoding="utf-8") as out:
        for file_path in files:
            data = parse_lean_file(file_path, relative_to=search)
            if not data["metadata"]:
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError:
                continue

            header, formal = _extract_theorem_info(content)
            problem_name = file_path.stem
            nl_proof = _find_nl_proof(base, problem_name)
            statement = _find_statement(base, problem_name)

            entry = {
                "id": problem_name,
                "file": data["file"],
                **data["metadata"],
                "statement": statement,
                "header": header,
                "formal": formal,
                "nl_proof": nl_proof,
            }
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def check_no_sorry(base_dir: str | Path) -> list[Path]:
    """Check for files containing 'sorry'."""
    base = Path(base_dir)
    files_with_sorry = []

    for file_path in find_lean_files(base):
        result = parse_lean_file(file_path, relative_to=base)
        if result["has_sorry"]:
            files_with_sorry.append(file_path.relative_to(base))

    return files_with_sorry
