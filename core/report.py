import re
from typing import List

from core.state import IssueItem

STATUS_LABELS = {
    "verified": "Verified: re-scan shows the finding is gone and nothing new appeared",
    "not_fixed": "Not verified: the original finding is still reported after the fix",
    "introduced_new_issues": "Not verified: the fix introduces new findings",
    "invalid_syntax": "Not verified: the patched file has a syntax error",
    "no_fix": "No usable fix was returned by the model",
    "no_change": "Not verified: the proposed fix does not change the code",
    "suppressed": "Needs human review: the fix only silences the scanner with a comment",
    "unverified": "Unverified: verification was turned off",
    "error": "Verification could not run (see log)",
    "skipped": "No AI fix was attempted for this finding",
}

def fence(text: str, lang: str = "python") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text or "")), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"

def build_markdown_report(issues: List[IssueItem], files: List[str], errors: List[str]) -> str:
    high = sum(1 for i in issues if i["severity"] == "High")
    med = sum(1 for i in issues if i["severity"] == "Medium")
    low = sum(1 for i in issues if i["severity"] == "Low")
    attempted = sum(1 for i in issues if i.get("fix_attempts", 0) > 0)
    verified = sum(1 for i in issues if i.get("fix_status") == "verified")

    lines = [
        "# AI Code Review Report",
        f"**Files Scanned:** {len(files)} | **Total Findings:** {len(issues)}",
        f"- **High Severity:** {high}",
        f"- **Medium Severity:** {med}",
        f"- **Low Severity:** {low}",
    ]
    
    if attempted:
        lines.append(f"- **AI fixes verified by re-scan:** {verified} of {attempted}")
    
    if errors:
        lines.append("\n## Warnings")
        lines.extend(f"- {e}" for e in errors)
    
    lines.append("\n---\n")
    
    if not issues:
        if errors:
            lines.append("No findings were produced, but some tools failed (see warnings). "
                         "This does NOT mean the code is clean.")
        else:
            lines.append("No static analysis or security issues detected.")
        return "\n".join(lines)
    
    for idx, issue in enumerate(issues, 1):
        lines.append(
            f"## {idx}. [{issue['severity']}] {issue['tool']} ({issue['code']}) "
            f"— `{issue['relative_path']}` (Line {issue['line']})"
        )
        lines.append(f"**Static Analysis Finding:** {issue['message']}\n")
        lines.append(fence(issue.get("snippet", "")) + "\n")
        if issue.get("ai_explanation"):
            lines.append(f"### AI Explanation\n{issue['ai_explanation']}\n")
    
        status = issue.get("fix_status", "skipped")
        if issue.get("ai_fix"):
            if status == "verified":
                lines.append(
                    f"### Verified Fix (attempt {issue.get('fix_attempts', 1)})\n"
                    f"{fence(issue.get('fix_diff', ''), 'diff')}\n"
                )
            else:
                label = STATUS_LABELS.get(status, status)
                lines.append(f"### Suggested Fix: {label}\n{fence(issue['ai_fix'])}\n")
        if issue.get("fix_log"):
            lines.append("### Verification Log")
            lines.extend(f"- {entry}" for entry in issue["fix_log"])
            lines.append("")
        lines.append("---\n")
    return "\n".join(lines)
    