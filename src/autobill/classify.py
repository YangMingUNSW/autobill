"""AI classification of merchants the rules miss. See docs/notify.md#ai-分类.

A purchase is categorised by the author's rules.yaml, then the built-in rules, then the
answers stored here; if none has it, it is 未分类. Each merchant is asked once:

  1. all new merchants together, from the model's own knowledge (cheap);
  2. those it was not sure of, one at a time, with web search (ai.web_search).

A "high" answer from step 1 or a "high"/"medium" one from step 2 is used. Every answer,
unsure ones too, is stored in ai_categories, so a merchant is never asked again and a
report never changes from one run to the next; `autobill classify --retry` asks the
unsure ones again. A rule in rules.yaml always wins over a stored answer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from autobill.categorize import UNCATEGORISED, Rules
from autobill.config import AiConfig
from autobill.model import TxnType
from autobill.store.db import now
from autobill.suggest import (
    AnswerCutOff,
    CategorySuggester,
    MerchantInfo,
    SuggesterError,
    Verdict,
    keep_valid,
)

BATCH = 10  # merchants per step-1 request: the model thinks at length about each
USE_UNSEARCHED = {"high"}
USE_SEARCHED = {"high", "medium"}


@dataclass
class ClassifyResult:
    verdicts: dict[str, Verdict] = field(default_factory=dict)  # merchant -> final answer
    used: set[str] = field(default_factory=set)  # merchants whose answer now categorises them
    left: int = 0  # merchants still waiting for a later run (ai.per_run)
    error: SuggesterError | None = None  # the model failed part way; the rest wait


def choosable(categories: list[str]) -> list[str]:
    """Categories a model may pick: not the catch-all for payments whose shop is unknown."""
    return [c for c in categories if "未细分" not in c]


def pending_merchants(conn: sqlite3.Connection, rules: Rules) -> list[MerchantInfo]:
    """Uncategorised purchase merchants never asked before, the most frequent first.
    Only the name, the printed location and the currency are kept."""
    asked = {r[0] for r in conn.execute("SELECT merchant FROM ai_categories")}
    found: dict[str, MerchantInfo] = {}
    counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT description_raw, merchant, merchant_location, currency, orig_currency"
        " FROM transactions WHERE txn_type = ?",
        (TxnType.PURCHASE.value,),
    )
    for r in rows:
        if rules.categorize(r["description_raw"], TxnType.PURCHASE, r["merchant"]) != UNCATEGORISED:
            continue
        name = r["merchant"] or r["description_raw"]
        if name in asked:
            continue
        counts[name] = counts.get(name, 0) + 1
        location = r["merchant_location"] if r["merchant"] else None  # else it is in the name
        found.setdefault(name, MerchantInfo(name, location, r["orig_currency"] or r["currency"]))
    return sorted(found.values(), key=lambda m: (-counts[m.name], m.name))


def classify_merchants(
    conn: sqlite3.Connection,
    suggester: CategorySuggester,
    rules: Rules,
    config: AiConfig,
    *,
    limit: int | None = None,
    save: bool = True,
) -> ClassifyResult:
    """Ask about up to `limit` (default ai.per_run) new merchants. Answers are saved one
    by one; when the model fails part way, the answers before it are kept, the error is
    in `result.error` and the merchants not answered are asked on a later run."""
    pending = pending_merchants(conn, rules)
    todo = pending[: limit or config.per_run]
    result = ClassifyResult(left=len(pending) - len(todo))
    categories = choosable(rules.categories)
    try:
        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            first = _ask(suggester, batch, categories)
            for merchant in batch:
                verdict = first.get(merchant.name) or Verdict(None, "low", "AI 没有回答")
                if not _usable(verdict) and config.web_search:
                    asked = suggester.classify([merchant], categories, search=True)
                    verdict = keep_valid(asked, [merchant], categories).get(merchant.name, verdict)
                _record(conn, result, merchant, verdict, suggester.model, save)
    except SuggesterError as exc:
        result.error = exc
        result.left += len(todo) - len(result.verdicts)
    return result


def _ask(suggester, batch: list[MerchantInfo], categories: list[str]) -> dict[str, Verdict]:
    """Step 1 for a batch; one the model's answer does not fit is asked in halves."""
    try:
        return keep_valid(suggester.classify(batch, categories, search=False), batch, categories)
    except AnswerCutOff:
        if len(batch) == 1:
            raise
        half = len(batch) // 2
        return _ask(suggester, batch[:half], categories) | _ask(suggester, batch[half:], categories)


def _usable(verdict: Verdict) -> bool:
    wanted = USE_SEARCHED if verdict.searched else USE_UNSEARCHED
    return verdict.category is not None and verdict.confidence in wanted


def _record(conn, result, merchant: MerchantInfo, verdict: Verdict, model: str, save: bool):
    used = _usable(verdict)
    result.verdicts[merchant.name] = verdict
    if used:
        result.used.add(merchant.name)
    if save:
        conn.execute(
            """INSERT OR REPLACE INTO ai_categories
               (merchant, category, guess, confidence, reason, searched, location, currency,
                model, asked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                merchant.name,
                verdict.category if used else None,
                verdict.category,
                verdict.confidence,
                verdict.reason,
                int(verdict.searched),
                merchant.location,
                merchant.currency,
                model,
                now(),
            ),
        )


def forget_unsure(conn: sqlite3.Connection) -> int:
    """Drop the stored "not sure" answers, so those merchants are asked again."""
    return conn.execute("DELETE FROM ai_categories WHERE category IS NULL").rowcount
