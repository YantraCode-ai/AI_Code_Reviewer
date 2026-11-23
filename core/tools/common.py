import subprocess
from typing import Sequence


class ScannerError(RuntimeError):
    """Raised when a static analysis tool fails to run (as opposed to finding issues)."""

def run_scanner_command(
    cmd: Sequence[str],
    name: str,
    ok_codes: Sequence[int] = (0, 1),
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    
    try:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScannerError(f"{name} timed out after {timeout}s.") from exc
    except OSError as exc:
        raise ScannerError(f"{name} could not be started: {exc}") from exc
    
    stderr = (result.stderr or "").strip()
    if result.returncode not in ok_codes:
        raise ScannerError(
            f"{name} failed with exit code {result.returncode}: {stderr[:500]}"
        )
    if result.returncode != 0 and not (result.stdout or "").strip():
        raise ScannerError(f"{name} produced no output: {stderr[:500] or 'unknown error'}")
    return result
    