"""Screenshots of the month's e-mail for the README, made from invented data.

Nothing here comes from a statement: every card, merchant and amount below is made up,
so the pictures can be public. The data goes through the real code (save_bill, the
month's report, the templates), so the screenshots show the e-mail exactly as AutoBill
renders it. Run it again after the e-mail's design changes; see docs/development.md.

    uv run --with playwright python scripts/demo_screenshots.py

It needs a Chromium for Playwright (`uv run --with playwright playwright install
chromium` once), or an existing one via --browser. Offline: exchange rates are written
into the throw-away database, nothing is fetched.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CYCLES = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
SHOWN = "2026-09"  # the month the screenshots show
AUD_TO_CNY = Decimal("4.7050")


@dataclass(frozen=True)
class Merchant:
    description: str  # as a statement would print it; the rules categorise it
    amount: float  # a typical charge, in the card's currency
    per_month: int  # how often it is used in an ordinary month
    location: str | None = None


# (account, statement day, currency, merchants). All invented.
CARDS = [
    (
        "ABC:0001",
        5,
        "CNY",
        [
            Merchant("瑞幸咖啡", 16, 9),
            Merchant("美团外卖", 38, 8),
            Merchant("盒马鲜生", 186, 3),
            Merchant("滴滴出行", 27, 6),
            Merchant("京东商城", 239, 1),
            Merchant("国家电网电费", 168, 1),
        ],
    ),
    (
        "CCB:0004",
        12,
        "CNY",
        [
            Merchant("星巴克", 39, 4),
            Merchant("海底捞火锅", 312, 1),
            Merchant("中国移动话费", 88, 1),
            Merchant("UNIQLO 优衣库", 299, 1),
            Merchant("永辉超市", 142, 2),
        ],
    ),
    (
        "BOC:0005",
        20,
        "AUD",
        [
            Merchant("WOOLWORTHS METRO", 46, 4, "SYDNEY AU"),
            Merchant("COLES", 58, 2, "SYDNEY AU"),
            Merchant("NETFLIX.COM", 18.99, 1, "SYDNEY AU"),
            Merchant("UBER *TRIP", 24, 3, "SYDNEY AU"),
        ],
    ),
]

# The shown month differs from the months before it, so that the category notes and the
# trend have something to say: more eating out, a trip, fewer rides.
SEPTEMBER_EXTRA = {
    "ABC:0001": [Merchant("海底捞火锅", 420, 1), Merchant("喜茶", 28, 4)],
    "CCB:0004": [Merchant("携程旅行网", 1860, 1), Merchant("12306 铁路客服", 553, 1)],
}
SEPTEMBER_FEWER = {"滴滴出行": 2, "UBER *TRIP": 1}
# A phone bought in twelve instalments: part of what is owed, never counted as spending.
INSTALLMENT = {"CCB:0004": (Decimal("583.25"), "手机")}
MONTH_SCALE = {"2026-04": 0.92, "2026-05": 1.05, "2026-06": 0.97, "2026-07": 1.12,
               "2026-08": 0.88, "2026-09": 1.0}  # fmt: skip


def money(value: float) -> Decimal:
    return Decimal(f"{value:.2f}")


def build_bills(rng: random.Random):
    from autobill.model import Bill, BillBalance, Transaction, TxnType, make_txn_id

    bills = []
    previous_due: dict[str, Decimal] = {}
    for cycle in CYCLES:
        year, month = (int(x) for x in cycle.split("-"))
        for account, day, currency, merchants in CARDS:
            statement = date(year, month, day)
            start = (statement - timedelta(days=30)).replace(day=day) + timedelta(days=1)
            chosen = list(merchants)
            if cycle == SHOWN:
                chosen += SEPTEMBER_EXTRA.get(account, [])
            rows: list[tuple[date, Decimal, str, str | None]] = []
            for m in chosen:
                times = m.per_month
                if cycle == SHOWN and m.description in SEPTEMBER_FEWER:
                    times = SEPTEMBER_FEWER[m.description]
                elif m.per_month > 1:
                    times = max(1, m.per_month + rng.randint(-1, 1))
                for _ in range(times):
                    spread = 1 if m.per_month == 1 and m.amount < 200 else rng.uniform(0.8, 1.2)
                    amount = money(m.amount * spread * MONTH_SCALE[cycle])
                    when = start + timedelta(days=rng.randrange((statement - start).days))
                    rows.append((when, amount, m.description, m.location))
            rows.sort(key=lambda r: r[0])

            last4 = account.split(":")[1]
            bank = account.split(":")[0]
            prev = previous_due.get(account, money(sum(float(r[1]) for r in rows) * 0.95))
            # (date, amount, type, description, merchant, location, instalment)
            entries = [(start + timedelta(days=15), -prev, TxnType.REPAYMENT, "还款 谢谢",
                        None, None, None)]  # fmt: skip
            entries += [(w, a, TxnType.PURCHASE, d, d, loc, None) for w, a, d, loc in rows]
            if account in INSTALLMENT:  # owed this month, but not spending: 合计应还 > 消费
                n = CYCLES.index(cycle) + 3
                principal, what = INSTALLMENT[account]
                entries.append((statement, principal, TxnType.INSTALLMENT,
                                f"{what} 分期本金 第{n}/12期", None, None, f"{n}/12"))  # fmt: skip
            transactions = [
                Transaction(
                    line_no=line,
                    txn_id=make_txn_id(bank, account, when, amount, description, line),
                    trans_date=when,
                    post_date=when + timedelta(days=1),
                    txn_type=kind,
                    amount=amount,
                    currency=currency,
                    description_raw=description,
                    merchant=merchant,
                    merchant_location=location,
                    card_last4=last4,
                    installment=installment,
                )
                for line, (
                    when,
                    amount,
                    kind,
                    description,
                    merchant,
                    location,
                    installment,
                ) in enumerate(entries, start=1)
            ]
            charges = sum((t.amount for t in transactions if t.amount > 0), Decimal("0"))
            previous_due[account] = charges
            bills.append(
                Bill(
                    bank=bank,
                    account_id=account,
                    cards=[last4],
                    statement_date=statement,
                    period_start=start,
                    period_end=statement,
                    due_date=statement + timedelta(days=20),
                    email_date=statement + timedelta(days=1),
                    balances=[
                        BillBalance(
                            currency=currency,
                            previous_balance=prev,
                            new_charges=charges,
                            payments_credits=prev,
                            amount_due=charges,
                            min_payment=money(float(charges) * 0.1),
                        )
                    ],
                    transactions=transactions,
                    status="OK",
                    reported_at=datetime(year, month, 28, 9, 0),
                    source_message_id=f"<demo-{account}-{cycle}@example.invalid>",
                    source_sha256="0" * 64,
                    parser_name="demo",
                    parser_version=1,
                )
            )
    return bills


def build_database(folder: Path):
    """The invented bills in a throw-away database, with the AUD rates they need."""
    from autobill.store.db import connect, now, save_bill

    conn = connect(folder / "demo.db")
    conn.execute("BEGIN")
    for bill in build_bills(random.Random(20260924)):
        save_bill(conn, bill, None)
        if "AUD" in {b.currency for b in bill.balances}:
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates VALUES (?, 'AUD', ?, ?, 'frankfurter', ?)",
                (bill.email_date.isoformat(), str(AUD_TO_CNY), bill.email_date.isoformat(), now()),
            )
    conn.execute("COMMIT")
    return conn


def offline(url: str) -> str:
    raise OSError(f"the demo never goes online ({url})")


def render(conn):
    """(the month's e-mail, one standard statement) as HTML."""
    from autobill.categorize import load_rules
    from autobill.config import FxConfig
    from autobill.fx import FxRates
    from autobill.report.cycle import build_cycle_report, latest_bills, render_cycle_html
    from autobill.report.statement import render_statement_html
    from autobill.store.db import load_bill

    fx = FxRates(conn, FxConfig(), offline)
    rules = load_rules(conn)
    report = build_cycle_report(
        conn, SHOWN, fx, rules, today=date(2026, 10, 1), now=datetime(2026, 10, 1, 9, 30)
    )
    bill = load_bill(conn, latest_bills(conn, SHOWN)["ABC:0001"])
    return render_cycle_html(report), render_statement_html(bill, fx, rules)


def section(page, heading: str, *, max_height: int | None = None) -> dict:
    """The clip rectangle from a section heading to the bottom of its card."""
    box = page.evaluate(
        """(heading) => {
            const h = [...document.querySelectorAll('.sh')]
                .find(e => e.textContent.trim() === heading);
            const card = h.nextElementSibling;
            const a = h.getBoundingClientRect(), b = card.getBoundingClientRect();
            return {y: a.top + window.scrollY, bottom: b.bottom + window.scrollY};
        }""",
        heading,
    )
    height = box["bottom"] - box["y"] + 28
    if max_height:
        height = min(height, max_height)
    return {"x": 0, "y": box["y"] - 14, "width": page.viewport_size["width"], "height": height}


def shoot(email_html: str, statement_html: str, out: Path, browser: str | None) -> list[Path]:
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    written = []
    with sync_playwright() as p:
        chromium = p.chromium.launch(executable_path=browser) if browser else p.chromium.launch()
        for scheme in ("light", "dark"):
            page = chromium.new_page(
                viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme=scheme
            )
            page.set_content(email_html)
            written.append(out / f"email-{scheme}.png")
            page.screenshot(path=written[-1])  # the first screen, as the phone shows it
            if scheme == "light":
                for name, heading in (("spending", "本月消费"), ("trend", "近 6 个月")):
                    written.append(out / f"email-{name}.png")
                    page.screenshot(path=written[-1], clip=section(page, heading), full_page=True)
                page.click("label[for=tx]")  # unfold the transactions
                written.append(out / "email-transactions.png")
                page.screenshot(
                    path=written[-1], clip=section(page, "全部流水", max_height=620), full_page=True
                )
            page.close()
        # The statement's first cards: header, payment information, account summary.
        page = chromium.new_page(viewport={"width": 430, "height": 700}, device_scale_factor=2)
        page.set_content(statement_html)
        written.append(out / "statement.png")
        page.screenshot(path=written[-1])
        chromium.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    parser.add_argument("--browser", help="a Chromium/Chrome executable to use")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUTOBILL_DATA_DIR"] = tmp  # never the real data directory or rules
        os.environ.pop("AUTOBILL_RULES", None)
        sys.path.insert(0, str(ROOT / "src"))
        conn = build_database(Path(tmp))
        email_html, statement_html = render(conn)
        conn.close()
    for path in shoot(email_html, statement_html, args.out, args.browser):
        print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}"
              f"  {path.stat().st_size // 1024} KB")  # fmt: skip


if __name__ == "__main__":
    main()
