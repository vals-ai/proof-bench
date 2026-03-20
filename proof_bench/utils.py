import logging
import re

logger = logging.getLogger(__name__)


def _strip_response_and_format_proof(text: str) -> str | None:
    """Extract and clean Lean proof code from LLM response. Returns None if `axiom` or `local_instance` are detected."""
    if not text or not text.strip():
        return ""

    text = strip_comment_blocks(text)

    fenced_match = re.search(r"```(?:lean4?|lean)?\s*\n(.*?)\n```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced_match:
        text = fenced_match.group(1)
    else:
        generic_match = re.search(r"```\s*\n(.*?)\n```", text, flags=re.DOTALL)
        if generic_match:
            text = generic_match.group(1)
    text = text.strip()

    by_match = re.search(r"(?:(?<=\s)by(?=\s|$)|^by(?=\s|$))", text)
    if by_match:
        text = text[by_match.start() :]

    if "axiom" in text or "local_instance" in text:
        logger.info("WARNING: Detected an `axiom` in the code; the response is therefore marked invalid.")
        return None

    text = re.sub(r"^:=\s*", "", text).strip()

    if text and not text.lower().startswith("by"):
        lines = text.splitlines()
        if len(lines) == 1:
            text = f"by\n  {text}"
        else:
            text = "by\n  " + "\n  ".join(line.rstrip() for line in lines)

    return text


def _strip_leading_empty_lines(lines: list[str]) -> list[str]:
    """Remove leading empty lines from a list of lines."""
    result = lines[:]
    while result and not result[0].strip():
        result.pop(0)
    return result


def _detect_commented_lines(lines: list[str]) -> list[bool]:
    """Check which lines are inside Lean comment blocks (/- ... -/).

    Limitation: only detects /- that starts a line (after stripping). Mid-line
    block comment openings are silently ignored, so multi-line comments that
    begin in the middle of a line will not be stripped. Closing lines (-/) are
    also marked as commented, so any code after -/ on the same line is lost.
    Both are acceptable given Lean headers always place /- at line starts.
    """
    in_comment = False
    result = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("--") and not in_comment:
            result.append(True)
            continue

        if stripped.startswith("/-") and not in_comment:
            in_comment = True

        if "-/" in stripped and in_comment:
            result.append(True)
            in_comment = False
            continue

        result.append(in_comment)

    assert len(lines) == len(result), "The length of lines and result must be equal."

    return result


def strip_comment_blocks(code: str) -> str:
    """Remove all comments (both single-line -- and multi-line /- ... -/) from code."""
    lines = code.split("\n")
    in_comment_flags = _detect_commented_lines(lines)

    result_lines = []
    for line, is_in_comment in zip(lines, in_comment_flags, strict=True):
        if not is_in_comment:
            result_lines.append(line)

    return "\n".join(result_lines)
