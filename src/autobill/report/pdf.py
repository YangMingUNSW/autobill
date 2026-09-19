"""Printing the standard statement to PDF with a local Chromium browser (Edge or Chrome).

The browser prints exactly the HTML the reader would see, so HTML and PDF never diverge,
and no PDF library is needed. Without a browser (e.g. on CI) statements are HTML only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess]  # tests pass a fake subprocess.run
TIMEOUT_SECONDS = 90
WRITE_WAIT_SECONDS = 30  # on Windows the launcher can exit before its child has written

_WINDOWS_CANDIDATES = [
    r"Microsoft\Edge\Application\msedge.exe",
    r"Google\Chrome\Application\chrome.exe",
]
_PATH_NAMES = [
    "msedge",
    "microsoft-edge",
    "google-chrome",
    "chrome",
    "chromium",
    "chromium-browser",
]


class PdfError(RuntimeError):
    pass


def find_browser(configured: str | None = None) -> Path | None:
    """The configured browser if it exists, else Edge/Chrome in the usual places, else None."""
    if configured:
        path = Path(configured)
        return path if path.exists() else None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        for candidate in _WINDOWS_CANDIDATES:
            if base and (Path(base) / candidate).exists():
                return Path(base) / candidate
    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def html_to_pdf(
    html_path: Path,
    pdf_path: Path,
    browser: Path,
    run: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Print html_path to pdf_path. A throw-away profile keeps it independent of any
    browser window the user has open."""
    pdf_path = pdf_path.resolve()
    pdf_path.unlink(missing_ok=True)
    # The browser's crash reporter can outlive it and keep files open: ignore cleanup errors.
    with tempfile.TemporaryDirectory(prefix="autobill-pdf-", ignore_cleanup_errors=True) as profile:
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={pdf_path}",
            html_path.resolve().as_uri(),
        ]
        if sys.platform.startswith("linux"):
            # Ubuntu 24.04 blocks the user namespaces Chrome's sandbox needs (GitHub runners,
            # Oracle servers). The page is our own local HTML without scripts, so printing it
            # unsandboxed is acceptable.
            command.insert(1, "--no-sandbox")
            # Docker gives /dev/shm only 64 MB; Chrome would crash writing big pages there.
            command.insert(2, "--disable-dev-shm-usage")
        try:
            done = run(command, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            raise PdfError(f"browser did not finish within {TIMEOUT_SECONDS}s") from None
        if not _wait_for_pdf(pdf_path, sleep):
            detail = _tail(getattr(done, "stderr", b""))
            raise PdfError(
                f"browser did not write a PDF to {pdf_path}" + (f": {detail}" if detail else "")
            )


def _tail(stderr, limit: int = 300) -> str:
    """The end of the browser's error output, for the error message."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return " ".join((stderr or "").split())[-limit:]


def _wait_for_pdf(path: Path, sleep: Callable[[float], None], step: float = 0.25) -> bool:
    """True once the file is a PDF whose size has stopped changing."""
    last_size = -1
    for _ in range(int(WRITE_WAIT_SECONDS / step)):
        if path.exists():
            size = path.stat().st_size
            with path.open("rb") as f:
                is_pdf = f.read(5) == b"%PDF-"
            if is_pdf and size > 0 and size == last_size:
                return True
            last_size = size
        sleep(step)
    return False
