import json
import os
import sys
from typing import Any, Dict, List, Optional

from core.tools.common import ScannerError, run_scanner_command

SEVERITY_MAP = {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}

def _relpath(path: str, base: str) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path

def _is_test_file(rel_path: str) -> bool:
    p = rel_path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "/tests/" in f"/{p}"
    )

def run_bandit(target_dir: str, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    cmd = [sys.executable, "-m", "bandit"]
    if os.path.isdir(target_dir):
        cmd.append("-r")
    cmd += [target_dir, "-f", "json", "-q"]
    result = run_scanner_command(cmd, "Bandit")
    
    stdout = (result.stdout or "").strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ScannerError(f"Bandit returned invalid JSON: {exc}") from exc
    
    issues: List[Dict[str, Any]] = []
    for item in data.get("results", []):
        raw_sev = str(item.get("issue_severity", "MEDIUM")).upper()
        file_path = item.get("filename", "")
        rel_path = _relpath(file_path, base_dir or target_dir)
        if item.get("test_id") == "B101" and _is_test_file(rel_path):
            continue
        issues.append({
            "file_path": file_path,
            "relative_path": rel_path,
            "tool": "Bandit",
            "code": item.get("test_id", "B000"),
            "severity": SEVERITY_MAP.get(raw_sev, "Medium"),
            "line": item.get("line_number", 1),
            "message": item.get("issue_text", "Security vulnerability detected."),
        })
    return issues
    