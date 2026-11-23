from typing import List, TypedDict

RETRYABLE_STATUSES = {
    "no_fix", "no_change", "invalid_syntax", "not_fixed", "introduced_new_issues",
}

class IssueItem(TypedDict, total=False):
    file_path: str
    relative_path: str
    tool: str
    code: str
    severity: str
    line: int
    message: str
    snippet: str
    snippet_start: int
    snippet_end: int
    ai_explanation: str
    ai_fix: str

    fix_status: str
    fix_attempts: int
    fix_feedback: str
    fix_diff: str
    fix_log: List[str]

class ReviewState(TypedDict):
    target_dir: str
    files: List[str]
    provider: str
    model_name: str
    max_ai_issues: int
    verify_fixes: bool
    max_fix_attempts: int
    raw_issues: List[IssueItem]
    reviewed_issues: List[IssueItem]
    errors: List[str]
    markdown_report: str
