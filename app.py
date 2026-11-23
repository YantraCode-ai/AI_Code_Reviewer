import os
import zipfile

import streamlit as st
from dotenv import load_dotenv

from core.graph import build_review_graph
from core.report import STATUS_LABELS
from core.tools.file_handler import (
    WorkspaceError,
    cleanup_workspace,
    read_text_safe,
    save_pasted_code,
    save_uploaded_file,
)

load_dotenv()

st.set_page_config(page_title="AI Code Reviewer | Deterministic + Agentic", layout="wide")

def run_review(
    prepare_workspace,
    provider: str,
    model_name: str,
    max_ai_issues: int,
    verify_fixes: bool,
    max_fix_attempts: int,
) -> None:

    cleanup_workspace(st.session_state.pop("workspace", None))
    st.session_state.pop("review_result", None)

    try:
        workspace = prepare_workspace()
    except (WorkspaceError, zipfile.BadZipFile) as exc:
        st.error(f"Could not read the input: {exc}")
        return

    st.session_state["workspace"] = str(workspace)
    try:
        with st.spinner("Scanning, generating fixes, then re-scanning to verify them..."):
            graph = build_review_graph()
            st.session_state["review_result"] = graph.invoke({
                "target_dir": str(workspace),
                "files": [],
                "provider": provider,
                "model_name": model_name,
                "max_ai_issues": max_ai_issues,
                "verify_fixes": verify_fixes,
                "max_fix_attempts": max_fix_attempts,
                "raw_issues": [],
                "reviewed_issues": [],
                "errors": [],
                "markdown_report": "",
            })
    except Exception as exc:  # last-resort guard so the UI never white-screens
        st.error(f"Review pipeline failed: {exc}")


st.title("AI Code Reviewer")
st.caption("Deterministic Static Analysis (Ruff + Bandit) orchestrated with LangGraph & AI")

with st.sidebar:
    st.header("Engine Settings")
    provider_label = st.selectbox(
        "AI Provider",
        options=["Ollama (Local)", "OpenRouter (Cloud)", "Static Analysis Only (No AI)"],
        index=0,
    )

    if provider_label == "Ollama (Local)":
        provider = "ollama"
        model_name = st.text_input(
            "Ollama Model Name", value=os.getenv("DEFAULT_OLLAMA_MODEL", "qwen2.5-coder:7b")
        )
    elif provider_label == "OpenRouter (Cloud)":
        provider = "openrouter"
        model_name = st.text_input(
            "OpenRouter Model Name",
            value=os.getenv("DEFAULT_OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free"),
        )
    else:
        provider = "none"
        model_name = ""

    max_ai_issues = st.slider(
        "Max Issues to Explain with AI", 1, 15, 5,
        help="Caps the number of LLM calls per scan to keep reviews fast.",
    )
    no_ai = provider == "none"
    verify_fixes = st.checkbox(
        "Verify AI fixes by re-scanning",
        value=True,
        disabled=no_ai,
        help="Applies each proposed fix to a sandbox copy and re-runs Ruff + Bandit on it.",
    )
    max_fix_attempts = st.slider(
        "Max fix attempts per issue",
        1, 3, 2,
        disabled=no_ai or not verify_fixes,
        help="If verification fails, the failure is sent back to the AI for another try.",
    )

tab_paste, tab_upload = st.tabs(["Paste or Demo Code", "Upload File / Project ZIP"])

with tab_paste:
    code_input = st.text_area(
        "Python Source Code", value="", height=240, placeholder="Paste your Python code here.....",
    )
    if st.button("Run Code Review", type="primary", key="btn_paste"):
        if code_input.strip():
            run_review(
                lambda: save_pasted_code(code_input, "demo_module.py"),
                provider, model_name, max_ai_issues, verify_fixes, max_fix_attempts,
            )
        else:
            st.warning("Please paste some Python code first.")

with tab_upload:
    uploaded_file = st.file_uploader(
        "Upload a `.py` file or `.zip` project archive", type=["py", "zip"]
    )
    if st.button("Scan Uploaded Project", type="primary", key="btn_upload"):
        if uploaded_file is not None:
            run_review(
                lambda: save_uploaded_file(uploaded_file),
                provider, model_name, max_ai_issues, verify_fixes, max_fix_attempts,
            )
        else:
            st.warning("Please upload a .py or .zip file first.")

if "review_result" in st.session_state:
    res = st.session_state["review_result"]
    issues = res.get("reviewed_issues", [])
    files = res.get("files", [])
    errors = res.get("errors", [])

    st.divider()

    for message in errors:
        st.warning(message)

    high = sum(1 for i in issues if i["severity"] == "High")
    med = sum(1 for i in issues if i["severity"] == "Medium")
    low = sum(1 for i in issues if i["severity"] == "Low")

    verified = sum(1 for i in issues if i.get("fix_status") == "verified")
    attempted = sum(1 for i in issues if i.get("fix_attempts", 0) > 0)
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Files Scanned", len(files))
    m2.metric("Total Issues", len(issues))
    m3.metric("High", high)
    m4.metric("Medium", med)
    m5.metric("Low", low)
    m6.metric("Verified fixes", f"{verified}/{attempted}")

    st.download_button(
        label="Download Full Review Report (.md)",
        data=res.get("markdown_report", ""),
        file_name="code_review_report.md",
        mime="text/markdown",
    )
    
    left_col, right_col = st.columns([1, 1.15])
    
    with left_col:
        st.subheader("Source Code")
        if files:
            rel_map = {os.path.relpath(f, res["target_dir"]): f for f in files}
            selected_rel = st.selectbox("Select File", options=list(rel_map.keys()))
            raw_source = read_text_safe(rel_map[selected_rel])
            numbered = "\n".join(
                f"{idx + 1:4d} | {line}" for idx, line in enumerate(raw_source.splitlines())
            )
            st.code(numbered, language="python")
    
    with right_col:
        st.subheader("Findings & AI Fixes")
        if not issues:
            if errors:
                st.error("No findings, but some tools failed. This does NOT mean the code is clean.")
            else:
                st.success("Clean code! Neither Ruff nor Bandit flagged any issues.")
        else:
            for idx, issue in enumerate(issues, 1):
                sev = issue["severity"]
                header = (
                    f"[{sev.upper()}] {issue['tool']} {issue['code']} — "
                    f"{issue['relative_path']} (Line {issue['line']})"
                )
                {"High": st.error, "Medium": st.warning}.get(sev, st.info)(header)
    
                with st.expander(f"Inspect Issue #{idx}: {issue['message']}", expanded=(idx == 1)):
                    st.markdown("**Flagged Code Context:**")
                    st.code(issue["snippet"], language="python")
                    if issue.get("ai_explanation"):
                        st.markdown("**AI Root-Cause Explanation:**")
                        st.write(issue["ai_explanation"])
                    status = issue.get("fix_status", "skipped")
                    if issue.get("ai_fix"):
                        if status == "verified":
                            st.success(
                                f"Fix verified by re-scan (attempt {issue.get('fix_attempts', 1)})"
                            )
                            st.markdown("**Verified Patch:**")
                            st.code(issue.get("fix_diff", ""), language="diff")
                        else:
                            st.warning(STATUS_LABELS.get(status, status))
                            st.markdown("**Suggested Fix (not verified):**")
                            st.code(issue["ai_fix"], language="python")
                    if issue.get("fix_log"):
                        st.markdown("**Verification log:**")
                        for entry in issue["fix_log"]:
                            st.caption(entry)
    