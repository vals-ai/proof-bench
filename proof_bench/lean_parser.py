from pathlib import Path
from typing import Any

HEADER_FIELDS = ["Source:"]


def extract_metadata(text: str) -> dict[str, Any] | None:
    """Parse required metadata fields from a Lean file header."""
    if not text or not text.strip():
        return None

    meta = {}
    lines = text.splitlines()

    for field in HEADER_FIELDS:
        field_lower = field[:-1].lower()
        found = False

        for line in lines:
            line = line.strip()
            if line.startswith(field):
                if ":" in line:
                    content = line.split(":", 1)[1].strip()
                    meta[field_lower] = content
                    found = True
                    break

        if not found:
            return None

    return meta if len(meta) == len(HEADER_FIELDS) else None


def find_lean_files(base_dir: Path) -> list[Path]:
    """Find all Lean files excluding lakefile, .lake/, .venv/, and solutions/ directories."""
    excluded_patterns = {".lake", ".venv", "solutions"}

    return [
        p
        for p in base_dir.rglob("*.lean")
        if not any(part in excluded_patterns for part in p.parts) and "lakefile" not in p.name
    ]


def has_sorry(text: str) -> bool:
    """Check if text contains 'sorry'."""
    return "sorry" in text.lower()


def parse_lean_file(path: Path, relative_to: Path | None = None) -> dict[str, Any]:
    """Parse a Lean file and extract metadata."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return {
            "file": str(path.relative_to(relative_to) if relative_to else path),
            "metadata": None,
            "has_sorry": False,
            "error": f"Failed to read file: {e}",
        }

    metadata = extract_metadata(text)
    file_path = path.relative_to(relative_to) if relative_to else path

    return {
        "file": str(file_path),
        "metadata": metadata,
        "has_sorry": has_sorry(text),
    }
