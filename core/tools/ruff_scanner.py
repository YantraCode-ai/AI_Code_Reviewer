import json
import os
import sys
from typing import Any, Dict, List, Optional

from core.tools.common import ScannerError, run_scanner_command

def _relpath(path: str, base: str) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:  
        return path

def run_ruff(target_dir: str, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    cmd = [
        sys.executable, "-m", "ruff", "check", target_dir,
        "--isolated",              # ignore stray pyproject.toml / ruff.toml
        "--no-cache",
        "--no-respect-gitignore",  # never skip files because of a .gitignore
        "--select=E,F,B,W",
        "--ignore=E501",           # line-too-long is noise for this tool
        "--output-format=json",
    ]
    result = run_scanner_command(cmd, "Ruff")

    stdout = (result.stdout or "").strip()
    if not stdout:
        return []
    try:
        raw_findings = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ScannerError(f"Ruff returned invalid JSON: {exc}") from exc
    
    issues: List[Dict[str, Any]] = []
    for item in raw_findings:
        rule_code = item.get("code") or "SYNTAX"
        severity = (
            "Medium"
            if rule_code.startswith(("B", "F8", "E9")) or rule_code == "SYNTAX"
            else "Low"
        )
        file_path = item.get("filename", "")
        location = item.get("location") or {}
        issues.append({
            "file_path": file_path,
            "relative_path": _relpath(file_path, base_dir or target_dir),
            "tool": "Ruff",
            "code": rule_code,
            "severity": severity,
            "line": location.get("row", 1),
            "message": item.get("message", "Code quality issue detected."),
        })
    return issues
    