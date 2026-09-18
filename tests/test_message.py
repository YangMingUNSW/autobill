import hashlib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from autobill.fetch.message import BEIJING, RawMessage

FIXTURES = Path(__file__).parent / "fixtures"


def test_abc_fixture():
    data = (FIXTURES / "abc" / "abc_unionpay_2026-09.eml").read_bytes()
    msg = RawMessage.from_bytes(data)
    assert msg.sha256 == hashlib.sha256(data).hexdigest()
    assert msg.from_addr == "e-statement@creditcard.abchina.com.cn"
    assert msg.subject == "中国农业银行金穗信用卡电子对账单"
    assert msg.message_id.startswith("<") and msg.message_id.endswith(">")
    assert msg.sent_at == datetime(2026, 9, 18, 5, 18, 28, tzinfo=BEIJING)
    assert "账务说明" in msg.html
    assert msg.attachments == []


def test_boc_pdf_attachment_found_by_content():
    msg = RawMessage.from_bytes((FIXTURES / "boc" / "boc_visa_2026-08.eml").read_bytes())
    pdfs = [a for a in msg.attachments if a.is_pdf]
    assert len(pdfs) == 1
    assert pdfs[0].content_type == "application/octet-stream"  # not application/pdf
    assert msg.html  # the HTML notice is still a body part


def build(date_header: str | None = None, body: bytes = b"hello", charset: str = "utf-8") -> bytes:
    msg = EmailMessage()
    msg["From"] = "Bank <Service@Example.com>"
    msg["Subject"] = "statement"
    if date_header:
        msg["Date"] = date_header
    msg.set_content(body, maintype="text", subtype="html", params={"charset": charset})
    return msg.as_bytes()


def test_email_date_is_beijing_day():
    # 20:30 UTC on 1 Sep is already 2 Sep in Beijing: that decides the FX date.
    msg = RawMessage.from_bytes(build("Mon, 01 Sep 2026 20:30:00 +0000"))
    assert str(msg.email_date) == "2026-09-02"
    assert msg.from_addr == "service@example.com"


def test_missing_date_and_message_id():
    msg = RawMessage.from_bytes(build())
    assert msg.sent_at is None and msg.email_date is None
    assert msg.message_id == f"sha256:{msg.sha256}"


def test_mislabelled_charset_falls_back_to_gb18030():
    body = "账务说明".encode("gbk")
    msg = RawMessage.from_bytes(build(body=body, charset="utf-8"))
    assert "账务说明" in msg.html
