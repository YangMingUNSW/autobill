"""Merchants the rules miss, ready to paste into rules.yaml (`autobill uncategorised`).
See docs/notify.md#分类规则.

Most spending repeats at the same places, so each merchant needs a rule only once. The
listing is ranked by CNY so the few merchants that matter most come first.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from autobill.categorize import UNCATEGORISED, Rules
from autobill.fx import FxRates, RateUnavailable
from autobill.model import ZERO, TxnType
from autobill.report.monthly import cents, month_bounds
from autobill.report.style import money


@dataclass
class Unknown:
    name: str
    count: int = 0
    cny: Decimal = ZERO
    unrated: dict[str, Decimal] = field(default_factory=dict)  # currency -> amount, no rate

    @property
    def amount_text(self) -> str:
        parts = [f"¥{money(cents(self.cny))}"] if self.cny else []
        parts += [f"{c} {money(v)}" for c, v in self.unrated.items()]
        return " + ".join(parts) or "¥0.00"


def uncategorised_merchants(
    conn: sqlite3.Connection, fx: FxRates, rules: Rules, cycle: str | None = None
) -> list[Unknown]:
    """Purchases no rule matches, grouped by merchant, largest CNY total first.
    `cycle` limits them to the statements issued in that month."""
    query = (
        "SELECT t.description_raw, t.merchant, t.amount, t.currency, b.email_date"
        " FROM transactions t JOIN bills b ON b.id = t.bill_id WHERE t.txn_type = ?"
    )
    args: list = [TxnType.PURCHASE.value]
    if cycle:
        start, end = month_bounds(cycle)
        query += " AND b.statement_date >= ? AND b.statement_date < ?"
        args += [start.isoformat(), end.isoformat()]
    found: dict[str, Unknown] = {}
    for r in conn.execute(query, args):
        if rules.categorize(r["description_raw"], TxnType.PURCHASE, r["merchant"]) != UNCATEGORISED:
            continue
        name = r["merchant"] or r["description_raw"]
        entry = found.setdefault(name, Unknown(name))
        entry.count += 1
        amount = Decimal(r["amount"])
        try:
            rate = fx.rate(r["currency"], date.fromisoformat(r["email_date"]))
            entry.cny += amount * rate.rate_to_cny
        except RateUnavailable:
            entry.unrated[r["currency"]] = entry.unrated.get(r["currency"], ZERO) + amount
    return sorted(found.values(), key=lambda u: (-u.cny, u.name))


def rules_snippet(unknowns: list[Unknown]) -> str:
    """YAML to paste into rules.yaml: each merchant listed for the author to sort."""
    out = [
        "# 复制到 rules.yaml（数据目录里没有这个文件就新建一个；内置规则照样生效）。",
        '# 可以把商户名改短，或者改成通用词，比如 "word:KELLYS"。见 docs/notify.md#分类规则',
    ]
    if unknowns:
        out += ["", "# 还没归类：把每一行挪到合适的分类下面"]
        out += [
            f"#  - {json.dumps(u.name, ensure_ascii=False)}   # {u.count} 笔 {u.amount_text}"
            for u in unknowns
        ]
    return "\n".join(out)
