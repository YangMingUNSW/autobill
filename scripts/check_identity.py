"""Block identity data from entering this public repository.

Used by pre-commit (staged files are passed as arguments) and by CI (`--all`).
See docs/security.md and docs/development.md §4.

Checks:
  * Path rules: statements (.eml / PDF, detected by content) only under
    tests/fixtures/; database files are never allowed.
  * Content rules: 18-digit resident ID numbers (checksum-validated),
    11-digit mainland mobile numbers, and 16-19 digit card numbers
    (Luhn-validated). E-mails are MIME-decoded first, including nested
    message/rfc822 parts and PDF attachments, because base64 bodies hide
    their content from a plain text search.

Matches are printed masked, since CI logs of a public repo are public too.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import io
import re
import subprocess
import sys
from collections.abc import Iterator
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path, PurePosixPath

FIXTURES_DIR = PurePosixPath("tests/fixtures")
STATEMENT_SUFFIXES = {".eml", ".pdf"}
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm"}
# Machine-generated; full of hashes and byte sizes that look like phone/card numbers.
CONTENT_EXEMPT = {PurePosixPath("uv.lock")}

ID18_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?![\dA-Za-z])")
MOBILE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 16-19 digits, optionally grouped by single spaces or dashes ("6228 4812 ...").
CARD_RE = re.compile(r"(?<!\d)(\d(?:[ -]?\d){15,18})(?!\d)")

ID18_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
ID18_CHECK_CHARS = "10X98765432"


def id18_valid(value: str) -> bool:
    """GB 11643 checksum of an 18-character resident ID number."""
    total = sum(int(d) * w for d, w in zip(value[:17], ID18_WEIGHTS, strict=True))
    return ID18_CHECK_CHARS[total % 11] == value[17].upper()


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def mask(value: str) -> str:
    return f"{value[:3]}{'*' * (len(value) - 5)}{value[-2:]}"


def find_identity(text: str) -> Iterator[tuple[str, str]]:
    """Yield (rule, masked value) for every identity-looking number in text."""
    for m in ID18_RE.finditer(text):
        if id18_valid(m.group(1)):
            yield "id-number", mask(m.group(1))
    for m in MOBILE_RE.finditer(text):
        yield "mobile", mask(m.group(1))
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(1))
        if luhn_valid(digits):
            yield "card-number", mask(digits)


def pdf_text(data: bytes) -> str:
    import pdfplumber  # dev dependency; imported lazily so path checks work without it

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def message_texts(msg: Message) -> Iterator[str]:
    """Decoded headers and bodies of every part, including nested rfc822 messages and PDFs."""
    # walk() also descends into message/rfc822 parts, so forwarded originals are covered.
    for part in msg.walk():
        for key, value in part.items():
            try:
                yield f"{key}: {make_header(decode_header(str(value)))}"
            except Exception:
                yield f"{key}: {value}"
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if payload.startswith(b"%PDF-"):
            yield pdf_text(payload)
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            yield payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            yield payload.decode("gb18030", errors="replace")


def file_texts(data: bytes, suffix: str) -> Iterator[str]:
    if data.startswith(b"%PDF-"):
        yield pdf_text(data)
    elif suffix == ".eml":
        yield from message_texts(email.message_from_bytes(data, policy=email.policy.compat32))
    elif b"\x00" not in data[:8192]:
        yield data.decode("utf-8", errors="replace")


def check_file(path: Path, rel: PurePosixPath) -> list[str]:
    problems: list[str] = []
    suffix = rel.suffix.lower()
    if suffix in DATABASE_SUFFIXES:
        return [f"{rel}: database files must never be committed"]
    data = path.read_bytes()
    is_statement = suffix in STATEMENT_SUFFIXES or data.startswith(b"%PDF-")
    if is_statement and FIXTURES_DIR not in rel.parents:
        problems.append(f"{rel}: statements (.eml/.pdf) are only allowed under {FIXTURES_DIR}/")
    if rel in CONTENT_EXEMPT:
        return problems
    seen: set[tuple[str, str]] = set()
    for text in file_texts(data, suffix):
        for hit in find_identity(text):
            if hit not in seen:
                seen.add(hit)
                problems.append(f"{rel}: possible {hit[0]} {hit[1]}")
    return problems


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return Path(out.stdout.strip())


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
    )
    return [p for p in out.stdout.split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="files to check (relative to the repo root)")
    parser.add_argument("--all", action="store_true", help="check every tracked file")
    args = parser.parse_args(argv)

    root = repo_root()
    files = tracked_files(root) if args.all else args.files
    problems: list[str] = []
    for name in files:
        path = root / name
        if path.is_file():
            problems.extend(check_file(path, PurePosixPath(Path(name).as_posix())))

    for line in problems:
        print(line)
    if problems:
        print(
            f"\n{len(problems)} problem(s). Remove the identity data (see docs/security.md), "
            "or fix the file location.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
