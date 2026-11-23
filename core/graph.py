from concurrent.futures import ThreadPoolExecutor

from langgraph.graph import END, START, StateGraph

from core.agent import check_provider, get_llm, needs_retry, retry_issues, review_issues
from core.report import build_markdown_report
from core.state import ReviewState
from core.tools.bandit_scanner import run_bandit
from core.tools.common import ScannerError
from core.tools.file_handler import get_code_snippet, get_python_files, get_snippet_range
from core.tools.ruff_scanner import run_ruff
from core.verifier import dedupe_issues, verify_issue_fix

SEVERITY_RANK = {"High": 0, "Medium": 1, "Low": 2}
DEFAULT_MAX_FIX_ATTEMPTS = 2

def discover_files_node(state: ReviewState) -> dict:
    return {"files": get_python_files(state["target_dir"])}

def static_analysis_node(state: ReviewState) -> dict:
    target_dir = state["target_dir"]
    errors = list(state.get("errors", []))

    if not state.get("files"):
        errors.append("No Python files found to scan.")
        return {"raw_issues": [], "errors": errors}

    combined = []
    for scanner in (run_bandit, run_ruff):
        try:
            combined.extend(scanner(target_dir))
        except ScannerError as exc:
            errors.append(str(exc))

    unique = dedupe_issues(combined)
    unique.sort(
        key=lambda x: (SEVERITY_RANK.get(x["severity"], 3), x["relative_path"], x["line"])
    )
    for issue in unique:
        issue["snippet"] = get_code_snippet(issue["file_path"], issue["line"])
        issue["snippet_start"], issue["snippet_end"] = get_snippet_range(
            issue["file_path"], issue["line"]
        )
        issue["ai_explanation"] = ""
        issue["ai_fix"] = ""
        issue["fix_status"] = "skipped"
        issue["fix_attempts"] = 0
        issue["fix_feedback"] = ""
        issue["fix_diff"] = ""
        issue["fix_log"] = []

    return {"raw_issues": unique, "errors": errors}

def ai_review_node(state: ReviewState) -> dict:
    raw_issues = state.get("raw_issues", [])
    provider = state.get("provider", "none")
    errors = list(state.get("errors", []))

    if provider == "none" or not raw_issues:
        return {"reviewed_issues": raw_issues}

    try:
        check_provider(provider, state["model_name"])
        llm = get_llm(provider, state["model_name"])
    except Exception as exc:
        errors.append(f"AI review skipped: {exc}")
        return {"reviewed_issues": [dict(i) for i in raw_issues], "errors": errors}

    reviewed = review_issues(raw_issues, llm, state.get("max_ai_issues", 5), provider)
    return {"reviewed_issues": reviewed}

def verify_fixes_node(state: ReviewState) -> dict:
    issues = [dict(i) for i in state.get("reviewed_issues", [])]

    if state.get("provider", "none") == "none" or not state.get("verify_fixes", True):
        for issue in issues:
            if issue.get("fix_status") == "pending":
                issue["fix_status"] = "unverified"
        return {"reviewed_issues": issues}

    by_file = {}
    for issue in issues:
        by_file.setdefault(issue["relative_path"], []).append(issue)

    idxs = [n for n, i in enumerate(issues) if i.get("fix_status") == "pending"]
    if idxs:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda n: verify_issue_fix(issues[n], by_file[issues[n]["relative_path"]]), idxs
            ))
        for n, res in zip(idxs, results):
            issues[n] = res
    return {"reviewed_issues": issues}

def route_after_verify(state: ReviewState) -> str:
    if state.get("provider", "none") == "none" or not state.get("verify_fixes", True):
        return "compile_report"
    max_attempts = state.get("max_fix_attempts", DEFAULT_MAX_FIX_ATTEMPTS)
    if any(needs_retry(i, max_attempts) for i in state.get("reviewed_issues", [])):
        return "retry_fixes"
    return "compile_report"

def retry_fixes_node(state: ReviewState) -> dict:
    issues = [dict(i) for i in state.get("reviewed_issues", [])]
    errors = list(state.get("errors", []))
    max_attempts = state.get("max_fix_attempts", DEFAULT_MAX_FIX_ATTEMPTS)

    try:
        llm = get_llm(state["provider"], state["model_name"])
    except Exception as exc:
        errors.append(f"Fix retry skipped: {exc}")
        for issue in issues:
            if needs_retry(issue, max_attempts):
                issue["fix_status"] = "error"
        return {"reviewed_issues": issues, "errors": errors}

    return {"reviewed_issues": retry_issues(issues, llm, state["provider"], max_attempts)}

def compile_report_node(state: ReviewState) -> dict:
    report = build_markdown_report(
        state.get("reviewed_issues", []),
        state.get("files", []),
        state.get("errors", []),
    )
    return {"markdown_report": report}

def build_review_graph():
    workflow = StateGraph(ReviewState)
    workflow.add_node("discover_files", discover_files_node)
    workflow.add_node("static_analysis", static_analysis_node)
    workflow.add_node("ai_review", ai_review_node)
    workflow.add_node("verify_fixes", verify_fixes_node)
    workflow.add_node("retry_fixes", retry_fixes_node)
    workflow.add_node("compile_report", compile_report_node)

    workflow.add_edge(START, "discover_files")
    workflow.add_edge("discover_files", "static_analysis")
    workflow.add_edge("static_analysis", "ai_review")
    workflow.add_edge("ai_review", "verify_fixes")
    workflow.add_conditional_edges(
        "verify_fixes",
        route_after_verify,
        {"retry_fixes": "retry_fixes", "compile_report": "compile_report"},
    )
    workflow.add_edge("retry_fixes", "verify_fixes")
    workflow.add_edge("compile_report", END)
    return workflow.compile()
