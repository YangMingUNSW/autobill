"""Mail sources: where raw e-mails come from. See docs/fetcher.md#邮件源抽象mailsource."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawMail:
    data: bytes
    source: str  # where it came from, e.g. "dir:tests/fixtures/abc/abc_mc_2026-09.eml"


class DirectorySource:
    """Every *.eml file under a directory (recursively), in name order.

    Used by `autobill import-dir` and for offline development; no mailbox needed.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise NotADirectoryError(self.path)

    def iter_new(self) -> Iterator[RawMail]:
        for file in sorted(self.path.rglob("*.eml")):
            yield RawMail(file.read_bytes(), f"dir:{file.as_posix()}")
