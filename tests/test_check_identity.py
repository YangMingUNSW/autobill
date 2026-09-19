"""The identity scanner must be proven to fail on bad input before we trust its "clean"."""

import base64
from email.message import EmailMessage
from pathlib import Path, PurePosixPath

import check_identity as ci
import pytest
from check_identity import ID18_CHECK_CHARS, ID18_WEIGHTS, check_file, luhn_valid

FIXTURES = Path(__file__).parent / "fixtures"

# Fake identity numbers are built at runtime so this file itself passes the scanner.


def fake_mobile() -> str:
    return "138" + "0013" + "8000"


def fake_id18() -> str:
    body = "110105" + "19491231" + "002"
    total = sum(int(d) * w for d, w in zip(body, ID18_WEIGHTS, strict=True))
    return body + ID18_CHECK_CHARS[total % 11]


def fake_card() -> str:
    body = "622848" + "123456789"
    return next(body + str(d) for d in range(10) if luhn_valid(body + str(d)))


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a single line of Helvetica text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def check(tmp_path: Path, rel: str, data: bytes) -> list[str]:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return check_file(path, PurePosixPath(rel))


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        (fake_mobile(), "mobile"),
        (fake_id18(), "id-number"),
        (fake_card(), "card-number"),
        (
            " ".join(fake_card()[i : i + 4] for i in range(0, 16, 4)) + fake_card()[16:],
            "card-number",
        ),
    ],
)
def test_plain_text_hits(tmp_path, value, rule):
    problems = check(tmp_path, "notes.md", f"contact: {value}\n".encode())
    assert any(rule in p for p in problems), problems


def test_hits_are_masked_in_output(tmp_path):
    value = fake_card()
    problems = check(tmp_path, "notes.md", value.encode())
    assert problems
    assert all(value not in p for p in problems)


def test_adjacent_dates_are_not_a_card_number(tmp_path):
    # BOC detail rows: transaction date then posting date. Joined, some of these pairs
    # are 16 digits that pass the Luhn check.
    text = "\n".join(f"2025-06-{d:02d} 2025-06-{d + 2:02d} 0006 12.34" for d in range(1, 28))
    assert check(tmp_path, "notes.md", text.encode()) == []


def test_random_digit_runs_are_not_flagged(tmp_path):
    # Invalid checksums: a message-id style run and an 18-digit number that is not an ID.
    text = "Message-ID: <1.1234567890123456780.JavaMail>  id 110105194912310021"
    assert check(tmp_path, "notes.md", text.encode()) == []


def test_base64_email_body_is_decoded(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "statement"
    msg.set_content(f"call {fake_mobile()}", cte="base64")
    raw = msg.as_bytes()
    assert fake_mobile().encode() not in raw  # only visible after decoding
    problems = check(tmp_path, "tests/fixtures/x/a.eml", raw)
    assert any("mobile" in p for p in problems), problems


def test_pdf_inside_forwarded_email_is_checked(tmp_path):
    inner = EmailMessage()
    inner["Subject"] = "original"
    inner.set_content("see attachment")
    # Octet-stream with a misleading name: detection must go by the %PDF- header.
    inner.add_attachment(
        minimal_pdf(fake_card()), maintype="application", subtype="octet-stream", filename="a.bin"
    )
    outer = EmailMessage()
    outer["Subject"] = "Fwd: original"
    outer.set_content("forwarded")
    outer.add_attachment(inner)
    problems = check(tmp_path, "tests/fixtures/x/fwd.eml", outer.as_bytes())
    assert any("card-number" in p for p in problems), problems


def test_encoded_header_is_decoded(tmp_path):
    word = base64.b64encode(f"tel {fake_mobile()}".encode()).decode()
    raw = f"Subject: =?utf-8?b?{word}?=\n\nbody\n".encode()
    problems = check(tmp_path, "tests/fixtures/x/h.eml", raw)
    assert any("mobile" in p for p in problems), problems


def test_statements_outside_fixtures_are_blocked(tmp_path):
    assert check(tmp_path, "samples/a.eml", b"Subject: x\n\nhello\n")
    # PDFs are recognised by content, not by name.
    assert check(tmp_path, "docs/scan.txt", minimal_pdf("hello"))
    assert check(tmp_path, "tests/fixtures/x/a.eml", b"Subject: x\n\nhello\n") == []


def test_databases_are_always_blocked(tmp_path):
    assert check(tmp_path, "tests/fixtures/autobill.db", b"SQLite format 3\x00")


def test_real_fixtures_are_clean():
    files = sorted(FIXTURES.rglob("*.eml"))
    assert len(files) >= 7
    for path in files:
        rel = PurePosixPath(path.relative_to(FIXTURES.parent.parent).as_posix())
        assert check_file(path, rel) == [], rel


# --- private terms (local list, never committed) ---------------------------------------


def private_list(tmp_path, *terms):
    path = tmp_path / "private-terms.txt"
    path.write_text(
        "# my private terms" + chr(10) + chr(10).join(terms) + chr(10), encoding="utf-8"
    )
    return ci.load_private_terms(path)


def test_private_card_digits_match_only_as_whole_numbers(tmp_path):
    terms = private_list(tmp_path, "4821")  # a made-up last-4, not a real card
    f = tmp_path / "notes.md"
    f.write_text('"CCB:4821": "CCB:0004"', encoding="utf-8")
    problems = ci.check_file(f, PurePosixPath("notes.md"), terms)
    assert problems == ["notes.md: possible private-term #1"]
    for harmless in ['"txn_id": "a672e80c482108ae"', "sha256:94821f", "2026-04-21"]:
        f.write_text(harmless, encoding="utf-8")
        assert ci.check_file(f, PurePosixPath("notes.md"), terms) == []


def test_private_words_match_case_insensitively_and_stay_masked(tmp_path):
    terms = private_list(tmp_path, "someone.bills@icloud.example")
    f = tmp_path / "a.py"
    f.write_text('TO = "Someone.Bills@iCloud.example"', encoding="utf-8")
    problems = ci.check_file(f, PurePosixPath("a.py"), terms)
    assert problems == ["a.py: possible private-term #1"]
    assert "someone" not in " ".join(problems).lower()


def test_no_private_list_means_no_private_check(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOBILL_PRIVATE_TERMS", str(tmp_path / "missing.txt"))
    assert ci.load_private_terms(ci.private_terms_path()) == []
