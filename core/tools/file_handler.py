import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple, Union

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ZIP_FILES = 2000
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
WORKSPACE_PREFIX = "code_review_"
SKIP_DIRS = {
    "__pycache__", ".venv", "venv", ".git", "node_modules",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
}

class WorkspaceError(ValueError):
    """Raised for invalid or unsafe user input (bad upload, zip bomb, etc.)."""

def create_workspace() -> Path:
    return Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX)).resolve()


def cleanup_workspace(path: Optional[Union[str, Path]]) -> None:
    if not path:
        return
    p = Path(path).resolve()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    if p.name.startswith(WORKSPACE_PREFIX) and p.parent == tmp_root and p.exists():
        shutil.rmtree(p, ignore_errors=True)

def save_pasted_code(code_str: str, filename: str = "snippet.py") -> Path:
    workspace = create_workspace()
    safe_name = Path(filename).name or "snippet.py"
    (workspace / safe_name).write_text(code_str, encoding="utf-8")
    return workspace

def _safe_extract_py_files(data: bytes, dest: Path) -> None:
    dest = dest.resolve()
    total_written = 0
    extracted = 0

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".py"):
                continue

            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest):
                continue

            extracted += 1
            if extracted > MAX_ZIP_FILES:
                raise WorkspaceError(f"Archive contains more than {MAX_ZIP_FILES} Python files.")

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    total_written += len(chunk)
                    if total_written > MAX_UNCOMPRESSED_BYTES:
                        raise WorkspaceError("Archive is too large when uncompressed.")
                    out.write(chunk)

def save_uploaded_file(uploaded_file) -> Path:
    name = Path(uploaded_file.name).name
    data = uploaded_file.getvalue()
    if len(data) > MAX_UPLOAD_BYTES:
        raise WorkspaceError("Uploaded file is too large (limit 20 MB).")

    lower = name.lower()
    if not lower.endswith((".py", ".zip")):
        raise WorkspaceError("Only .py and .zip files are supported.")

    workspace = create_workspace()
    try:
        if lower.endswith(".zip"):
            _safe_extract_py_files(data, workspace)
        else:
            (workspace / name).write_bytes(data)
    except Exception:
        cleanup_workspace(workspace)
        raise
    return workspace

def get_python_files(target_dir: str) -> List[str]:
    py_files = []
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for file in files:
            if file.endswith(".py"):
                py_files.append(os.path.join(root, file))
    return sorted(py_files)


def read_text_safe(file_path: Union[str, Path]) -> str:
    return Path(file_path).read_text(encoding="utf-8", errors="replace")

def get_snippet_range(
    file_path: str, line_number: int, context_lines: int = 6
) -> Tuple[int, int]:
    total = len(read_text_safe(file_path).splitlines())
    start = max(1, line_number - context_lines)
    end = min(total, line_number + context_lines)
    return start, max(start, end)


def get_code_snippet(file_path: str, line_number: int, context_lines: int = 6) -> str:
    try:
        lines = read_text_safe(file_path).splitlines()
        start = max(0, line_number - context_lines - 1)
        end = min(len(lines), line_number + context_lines)

        snippet_lines = []
        for idx in range(start, end):
            current_line_no = idx + 1
            marker = ">> " if current_line_no == line_number else "   "
            snippet_lines.append(f"{marker}{current_line_no:4d} | {lines[idx]}")
        return "\n".join(snippet_lines)
    except Exception as e:
        return f"# Unable to read snippet: {e}"

