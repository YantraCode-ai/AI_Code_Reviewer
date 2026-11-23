import ast
import difflib
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from core.tools.bandit_scanner import run_bandit
from core.tools.common import ScannerError
from core.tools.file_handler import cleanup_workspace, create_workspace, read_text_safe
from core.tools.ruff_scanner import run_ruff

# A fix that "passes" by muting the scanner is not a fix; it needs a human decision.
SUPPRESSION_RE = re.compile(r"#\s*(?:nosec|noqa|type:\s*ignore|pylint:\s*disable)", re.IGNORECASE)


@dataclass
class VerifyResult:
    status: str
    feedback: str
    diff: str = ""


def issue_key(issue: Dict[str, Any]) -> Tuple[str, str, str]:
    """Identity of a finding that survives line shifts caused by a patch."""
    return (issue["tool"], issue["code"], issue["message"])


def dedupe_issues(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, unique = set(), []
    for issue in issues:
        key = (issue["relative_path"], issue["line"], issue["code"], issue["tool"])
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def align_indent(original_lines: List[str], fixed_lines: List[str]) -> List[str]:
    """Models often dedent a mid-block snippet; shift it back to the original indentation."""
    orig = [ln for ln in original_lines if ln.strip()]
    fixed = [ln for ln in fixed_lines if ln.strip()]
    if not orig or not fixed:
        return fixed_lines
    delta = min(map(_indent_of, orig)) - min(map(_indent_of, fixed))
    if delta == 0:
        return fixed_lines

    out = []
    for ln in fixed_lines:
        if not ln.strip():
            out.append(ln)
        elif delta > 0:
            out.append(" " * delta + ln)
        else:
            out.append(ln[min(-delta, _indent_of(ln)):])
    return out


def apply_fix(original_text: str, start: int, end: int, fixed_code: str) -> str:
    """Replaces lines start..end (1-based, inclusive) with the fixed code."""
    lines = original_text.splitlines(keepends=True)
    if not (1 <= start <= end <= len(lines)):
        raise ValueError("snippet range is outside the file")

    window = lines[start - 1:end]
    fixed_lines = align_indent([ln.rstrip("\r\n") for ln in window], fixed_code.splitlines())
    block = "\n".join(fixed_lines)
    if fixed_lines and window[-1].endswith(("\n", "\r")):
        block += "\n"
    return "".join(lines[:start - 1]) + block + "".join(lines[end:])


def _describe(issues, limit: int = 3) -> str:
    return "; ".join(f"{i[0]} {i[1]}: {i[2]}" for i in list(issues)[:limit])


def verify_fix(issue: Dict[str, Any], fix_code: str, baseline: List[Dict[str, Any]]) -> VerifyResult:
    """Applies fix_code to a sandbox copy and re-scans. `baseline` = original findings of the file."""
    if not (fix_code or "").strip():
        return VerifyResult("no_fix", "The model did not return any corrected code.")

    start, end = issue.get("snippet_start"), issue.get("snippet_end")
    if start is None or end is None:
        return VerifyResult("error", "Unknown snippet range, cannot apply the fix.")

    original = read_text_safe(issue["file_path"])
    try:
        patched = apply_fix(original, start, end, fix_code)
    except ValueError as exc:
        return VerifyResult("error", f"Could not apply the fix: {exc}.")

    rel = issue["relative_path"]
    diff = "\n".join(difflib.unified_diff(
        original.splitlines(), patched.splitlines(),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=2,
    ))

    if patched == original:
        return VerifyResult("no_change", "Your fix is identical to the original code.")

    window_text = "\n".join(original.splitlines()[start - 1:end])
    if len(SUPPRESSION_RE.findall(fix_code)) > len(SUPPRESSION_RE.findall(window_text)):
        return VerifyResult(
            "suppressed",
            "The fix silences the scanner with a comment instead of changing the code. "
            "A human must decide whether this is a real false positive.",
            diff,
        )

    try:
        ast.parse(patched)
    except (SyntaxError, ValueError) as exc:
        detail = f"{exc.msg} (line {exc.lineno})" if isinstance(exc, SyntaxError) else str(exc)
        return VerifyResult("invalid_syntax", f"After applying your fix the file has a SyntaxError: {detail}.")

    sandbox = create_workspace()
    try:
        target = (sandbox / rel).resolve()
        if not target.is_relative_to(sandbox.resolve()):
            return VerifyResult("error", "Unsafe file path.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patched, encoding="utf-8")
        try:
            after = dedupe_issues(
                run_bandit(str(target), str(sandbox)) + run_ruff(str(target), str(sandbox))
            )
        except ScannerError as exc:
            return VerifyResult("error", f"Re-scan failed: {exc}")
    finally:
        cleanup_workspace(sandbox)

    before_c = Counter(issue_key(i) for i in baseline)
    after_c = Counter(issue_key(i) for i in after)
    target_key = issue_key(issue)
    still_there = after_c[target_key] >= before_c[target_key]
    new = after_c - before_c  # only keys whose count increased

    if still_there:
        msg = f"The original finding is still reported: {target_key[0]} {target_key[1]}: {target_key[2]}."
        if new:
            msg += f" It also introduces: {_describe(new)}."
        return VerifyResult("not_fixed", msg, diff)
    if new:
        return VerifyResult(
            "introduced_new_issues",
            f"The original finding is gone, but your fix introduces new findings: {_describe(new)}.",
            diff,
        )
    return VerifyResult("verified", "Re-scan: original finding gone, no new findings, file parses.", diff)


def verify_issue_fix(issue: Dict[str, Any], baseline: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Verifies issue['ai_fix'] and returns an updated copy of the issue."""
    result = dict(issue)
    log = list(issue.get("fix_log", []))
    try:
        vr = verify_fix(issue, issue.get("ai_fix", ""), baseline)
    except Exception as exc:  # never let one verification crash the whole pipeline
        vr = VerifyResult("error", f"Verification crashed: {exc}")

    result.update(fix_status=vr.status, fix_feedback=vr.feedback, fix_diff=vr.diff)
    log.append(f"Attempt {issue.get('fix_attempts', 1)}: {vr.status} - {vr.feedback}")
    result["fix_log"] = log
    return result