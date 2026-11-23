import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Tuple

from dotenv import load_dotenv
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from core.prompts import CODE_REVIEW_PROMPT, FIX_RETRY_PROMPT
from core.state import RETRYABLE_STATUSES, IssueItem

load_dotenv()

LLM_TIMEOUT_SECONDS = 120

class LLMUnavailableError(RuntimeError):
    """Raised when the selected LLM provider can't be reached / isn't configured."""


def _ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


def check_provider(provider: str, model_name: str) -> None:
    if provider == "ollama":
        url = f"{_ollama_base_url()}/api/tags"
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            raise LLMUnavailableError("OLLAMA_BASE_URL must start with http:// or https://")
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # nosec B310 - scheme validated
                tags = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise LLMUnavailableError(
                f"Cannot reach Ollama at {_ollama_base_url()} ({exc}). Is `ollama serve` running?"
            ) from exc

        installed = {m.get("name", "") for m in tags.get("models", [])}
        wanted = {model_name, f"{model_name}:latest"}
        if not installed & wanted:
            raise LLMUnavailableError(
                f"Model '{model_name}' is not installed. Run: ollama pull {model_name}"
            )
    elif provider == "openrouter":
        if not os.getenv("OPENROUTER_API_KEY"):
            raise LLMUnavailableError("OPENROUTER_API_KEY is missing in your .env file.")
    else:
        raise LLMUnavailableError(f"Unknown provider '{provider}'.")

def get_llm(provider: str, model_name: str):
    if provider == "ollama":
        return ChatOllama(
            model=model_name,
            base_url=_ollama_base_url(),
            temperature=0.1,
            client_kwargs={"timeout": LLM_TIMEOUT_SECONDS},
        )
    if provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not api_key:
            raise LLMUnavailableError("OPENROUTER_API_KEY is missing in your .env file.")
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url="https://api.apinex.bond/v1",
            temperature=0.1,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=1,
        )
    return None

def content_to_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
        return "".join(parts)
    return str(content)

_LINE_PREFIX_RE = re.compile(r"^(?:>> |   )?\s*\d+ \| ?")


def strip_line_numbers(text: str) -> str:
    lines = text.splitlines()
    non_empty = [ln for ln in lines if ln.strip()]
    if non_empty and all(_LINE_PREFIX_RE.match(ln) for ln in non_empty):
        return "\n".join(_LINE_PREFIX_RE.sub("", ln, count=1) for ln in lines)
    return text


_EXPLANATION_RE = re.compile(r"EXPLANATION:\s*(.*?)\s*FIXED_CODE:", re.DOTALL | re.IGNORECASE)
_FIXED_RE = re.compile(r"FIXED_CODE:\s*(.*)$", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[\w+-]*[ \t]*\n(.*?)(?:```|$)", re.DOTALL)


def parse_llm_response(text: str) -> Tuple[str, str]:
    text = text or ""
    exp_match = _EXPLANATION_RE.search(text)
    fix_match = _FIXED_RE.search(text)

    if not (exp_match and fix_match):
        return text.strip() or "No explanation returned.", ""

    explanation = exp_match.group(1).strip()
    raw_code = fix_match.group(1).strip()
    fence = _FENCE_RE.search(raw_code)
    fixed_code = (fence.group(1) if fence else raw_code).strip("\n").rstrip()
    return explanation, fixed_code

def _prompt_inputs(issue: IssueItem) -> dict:
    snippet = issue.get("snippet", "")
    flagged = next(
        (_LINE_PREFIX_RE.sub("", ln, count=1) for ln in snippet.splitlines() if ln.startswith(">>")),
        "",
    )
    return {
        "tool": issue["tool"],
        "code": issue["code"],
        "severity": issue["severity"],
        "message": issue["message"],
        "line": issue["line"],
        "snippet": strip_line_numbers(snippet),
        "flagged_code": flagged.strip(),
    }


def _set_proposal(result: dict, fix: str, attempt: int) -> None:
    result["ai_fix"] = strip_line_numbers(fix)
    result["fix_attempts"] = attempt
    result["fix_diff"] = ""
    if result["ai_fix"].strip():
        result["fix_status"] = "pending"  # waiting for verification
    else:
        result["fix_status"] = "no_fix"
        result["fix_feedback"] = "The model did not return any corrected code."
        result["fix_log"].append(f"Attempt {attempt}: no_fix - the model returned no code")


def _set_llm_error(result: dict, exc: Exception, attempt: int) -> None:
    result["fix_attempts"] = attempt
    result["fix_status"] = "error"
    result["fix_feedback"] = str(exc)
    result["fix_log"].append(f"Attempt {attempt}: error - {exc}")

def review_single_issue(issue: IssueItem, llm) -> IssueItem:
    result = dict(issue)
    result["fix_log"] = list(issue.get("fix_log", []))
    try:
        response = (CODE_REVIEW_PROMPT | llm).invoke(_prompt_inputs(issue))
        explanation, fix = parse_llm_response(content_to_str(response.content))
        result["ai_explanation"] = explanation
        _set_proposal(result, fix, 1)
    except Exception as e:
        result["ai_explanation"] = f"AI explanation unavailable: {e}"
        result["ai_fix"] = ""
        _set_llm_error(result, e, 1)
    return result

def retry_single_issue(issue: IssueItem, llm) -> IssueItem:
    result = dict(issue)
    result["fix_log"] = list(issue.get("fix_log", []))
    attempt = issue.get("fix_attempts", 1) + 1
    inputs = _prompt_inputs(issue)
    inputs["previous_fix"] = issue.get("ai_fix", "")
    inputs["feedback"] = issue.get("fix_feedback", "") or "The fix did not pass verification."
    try:
        response = (FIX_RETRY_PROMPT | llm).invoke(inputs)
        _, fix = parse_llm_response(content_to_str(response.content))
        _set_proposal(result, fix, attempt)
    except Exception as e:
        _set_llm_error(result, e, attempt)
    return result


def needs_retry(issue: IssueItem, max_attempts: int) -> bool:
    return (
        issue.get("fix_status") in RETRYABLE_STATUSES
        and issue.get("fix_attempts", 0) < max_attempts
    )


def _workers(provider: str) -> int:
    return 2 if provider == "ollama" else 4

def review_issues(issues: List[IssueItem], llm, max_ai: int, provider: str) -> List[IssueItem]:
    to_review, rest = issues[:max_ai], issues[max_ai:]
    with ThreadPoolExecutor(max_workers=_workers(provider)) as pool:
        reviewed = list(pool.map(lambda i: review_single_issue(i, llm), to_review))

    for issue in rest:
        item = dict(issue)
        item["ai_explanation"] = "Skipped AI generation (exceeded max AI issue limit per run)."
        item["ai_fix"] = ""
        item["fix_status"] = "skipped"
        item["fix_attempts"] = 0
        item["fix_log"] = []
        reviewed.append(item)
    return reviewed


def retry_issues(issues: List[IssueItem], llm, provider: str, max_attempts: int) -> List[IssueItem]:
    out = [dict(i) for i in issues]
    idxs = [n for n, i in enumerate(issues) if needs_retry(i, max_attempts)]
    if not idxs:
        return out
    with ThreadPoolExecutor(max_workers=_workers(provider)) as pool:
        results = list(pool.map(lambda n: retry_single_issue(issues[n], llm), idxs))
    for n, res in zip(idxs, results):
        out[n] = res
    return out
