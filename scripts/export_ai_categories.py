"""Export the AI's merchant categories so they can be turned into keyword rules.

The stored answers in `ai_categories` live only in the database on the server. Rules in
rules.example.yaml live in this public repository, so a merchant promoted to a rule is
backed up by Git and, being a rule, always wins over a stored answer
(see docs/notify.md#分类规则).

Read only: the database is opened with mode=ro, so this cannot change anything and is safe
to run while `serve` is going. Standard library only, so plain `python3` runs it on the
server without the project installed.

Privacy: `ai_categories.merchant` is the parser's merchant name, but falls back to the raw
description when the parser found no merchant (autobill/classify.py). A WeChat or Alipay
payment to a person prints that person's name in the description, and
tests/fixtures/README.md rules that other people's real names stay out of the repository.
So every row lands in one of three buckets:

  * held    - the name carries a payment-channel word (财付通, 支付宝, 转账, ...), or no
              transaction has it as a merchant at all. Never printed by default.
  * review  - the name could be a person: two or three Chinese characters and nothing else
              (which is also the shape of plenty of chains - 古茗, 海底捞), or it holds
              Chinese characters and is stored verbatim as a transaction's raw description.
              Never printed by default.
  * export  - everything else, printed for pasting into the repository.

What each check can and cannot do, since the buckets are only worth as much as this:

  * The payment-channel words are what actually hold back a person's name. A domestic
    WeChat or Alipay payment prints the channel in the description, so the name travels
    with a word from that list.
  * "No transaction has this name as a merchant" only catches rows whose `merchant` column
    is NULL. On the author's data 195 of 1,807 transactions store the whole raw description
    in `merchant` instead, and those rows pass this check - hence the raw-description test
    in the review bucket above.
  * Neither check knows a personal name written in Latin letters (a sole trader billing
    under their own name). Read the output before pasting it.

`--review` and `--show-held` print the other two buckets FOR READING ON THE SERVER; never
paste their output into the repository or into a chat. Held rows keep working on the server
either way: this script only reads.

Usage on the server (the script lives in the repository checkout, the database next to
compose.yaml, so both paths are spelled out):

    python3 ~/autobill/scripts/export_ai_categories.py ~/autobill-docker/data/autobill.db
    python3 ~/autobill/scripts/export_ai_categories.py ~/autobill-docker/data/autobill.db --yaml
    python3 ~/autobill/scripts/export_ai_categories.py ~/autobill-docker/data/autobill.db --review

Merchants the AI was unsure about (`category IS NULL`) are never exported: they are not
classifications, they count as 其他 until asked again.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Payment channels that print a person's name instead of a shop: the description reads
# "财付通-<name>" or "支付宝-<name>". Matched anywhere in the name.
CHANNEL_WORDS = re.compile(r"财付通|支付宝|微信|转账|转帐|代付|收款|汇款|个人")

# The bank's own markers around a merchant name, same two patterns as shown_name() in
# src/autobill/suggest.py (copied, not imported: this script stays standard-library only).
CHANNEL_PREFIX = re.compile(
    r"^(?:[A-Z]{3} )?(?:跨行无卡消费|跨行预授权完成|跨行消费|境外消费|网上消费|跨境消费)\s*"
)
WALLET_SUFFIX = re.compile(r"(?:[A-Z]{3})?(?:VISA|CUP|MC)\s*apple\s*pay.*$", re.IGNORECASE)

# A run of digits this long is a transaction or store number, not part of the name.
LONG_NUMBER = re.compile(r"\d{4,}")

# CJK Unified Ideographs, compared by code point: a character class holding the two range
# ends would put a barely-renderable literal in the source, which tools like to mangle.
CJK_FIRST, CJK_LAST = 0x4E00, 0x9FFF

EXPORT, REVIEW, HOLD = "export", "review", "hold"
SEVERITY = {EXPORT: 0, REVIEW: 1, HOLD: 2}


@dataclass
class Export:
    """One line of the export: a cleaned name, and how many stored spellings it covers."""

    name: str
    category: str
    location: str
    currency: str
    confidence: str
    variants: int = 1
    noisy: bool = False  # still carries a transaction number: worth shortening by hand


def clean_name(name: str) -> str:
    """The merchant name without the bank's payment markers, so one shop is one rule:
    "跨行消费 FAROS BROS PTY LTD MARRICKVILLEAUS" and "FAROS BROS PTY LTDAUSVISA Apple Pay"
    both come back as a name that matches every spelling of that shop."""
    cleaned = WALLET_SUFFIX.sub("", CHANNEL_PREFIX.sub("", name)).strip()
    return cleaned or name


def has_chinese(name: str) -> bool:
    return any(CJK_FIRST <= ord(c) <= CJK_LAST for c in name)


def is_bare_chinese_name(name: str) -> bool:
    """Two or three Chinese characters and nothing else - the shape of a personal name,
    but also of plenty of chains (古茗, 海底捞), so these are neither exported nor dropped."""
    return 2 <= len(name) <= 3 and all(CJK_FIRST <= ord(c) <= CJK_LAST for c in name)


def verdict_for(merchant: str, parsed_count: int, is_description: bool = False) -> tuple[str, str]:
    """Which bucket this merchant belongs in, and why."""
    if parsed_count == 0:
        return HOLD, "没有流水用这个商户名（说明它是原始描述）"
    if CHANNEL_WORDS.search(merchant):
        return HOLD, "名字里有支付渠道字样"
    if is_description and has_chinese(merchant):
        return REVIEW, "整条是原始描述，又带汉字：可能夹着人名"
    if is_bare_chinese_name(merchant):
        return REVIEW, "两三个汉字：可能是连锁店，也可能是人名"
    return EXPORT, ""


def verdict_both_ways(merchant: str, parsed_count: int, is_description: bool) -> tuple[str, str]:
    """The stricter of the verdicts on the cleaned name and on the stored one. Cleaning can
    only uncover a name ("网上消费 李四" -> "李四"), never hide one, so the stricter wins.

    Whether the row is a whole raw description is judged on the CLEANED name: the bank's own
    prefix ("跨行消费") is Chinese but is not part of the shop's name and says nothing about
    whose name is in there, and nearly every foreign purchase carries one."""
    cleaned = clean_name(merchant)
    verdicts = [verdict_for(cleaned, parsed_count, is_description)]
    if cleaned != merchant:
        verdicts.append(verdict_for(merchant, parsed_count, False))
    return max(verdicts, key=lambda v: SEVERITY[v[0]])


def rows_of(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.merchant, a.category, a.guess, a.confidence, a.location, a.currency,"
        " (SELECT COUNT(*) FROM transactions t WHERE t.merchant = a.merchant) AS parsed,"
        " (SELECT COUNT(*) FROM transactions t WHERE t.description_raw = a.merchant)"
        "   AS as_description"
        " FROM ai_categories a"
        " ORDER BY a.category IS NULL, a.category, a.merchant"
    ).fetchall()


def merge(rows: list[sqlite3.Row]) -> list[Export]:
    """One line per (cleaned name, category): the spellings of one shop become one rule."""
    grouped: dict[tuple[str, str], Export] = {}
    for row in rows:
        name = clean_name(row["merchant"])
        key = (name, row["category"])
        found = grouped.get(key)
        if found is None:
            grouped[key] = Export(
                name=name,
                category=row["category"],
                location=row["location"] or "",
                currency=row["currency"] or "",
                confidence=row["confidence"],
                noisy=bool(LONG_NUMBER.search(name)),
            )
            continue
        found.variants += 1
        found.location = found.location or (row["location"] or "")
        found.currency = found.currency or (row["currency"] or "")
    return sorted(grouped.values(), key=lambda e: (e.category, e.name))


def split(
    rows: list[sqlite3.Row],
) -> tuple[list[Export], list[tuple[sqlite3.Row, str]], list[tuple[sqlite3.Row, str]]]:
    """(exportable, [(review row, why)], [(held row, why)]). Unsure answers are in none."""
    safe: list[sqlite3.Row] = []
    review: list[tuple[sqlite3.Row, str]] = []
    held: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        bucket, why = verdict_both_ways(row["merchant"], row["parsed"], row["as_description"] > 0)
        if bucket == HOLD:
            held.append((row, why))
        elif not row["category"]:
            continue  # the AI was not sure: not a classification
        elif bucket == REVIEW:
            review.append((row, why))
        else:
            safe.append(row)
    return merge(safe), review, held


def remarks(item: Export) -> list[str]:
    """What the author needs to know about this line beyond the fields themselves."""
    out = []
    if item.variants > 1:
        out.append(f"覆盖 {item.variants} 种写法")
    if item.noisy:
        out.append("带流水号，建议改短")
    return out


def as_table(safe: list[Export]) -> list[str]:
    out = ["# 分类 | 商户名 | 地点 | 币种 | 把握"]
    for item in safe:
        fields = (item.category, item.name, item.location, item.currency, item.confidence)
        extra = remarks(item)
        out.append(" | ".join(fields) + (f"   （{'；'.join(extra)}）" if extra else ""))
    return out


def as_yaml(safe: list[Export]) -> list[str]:
    """Grouped by category, ready to merge into rules.example.yaml by hand."""
    out = [
        "# 合并进 rules.example.yaml 和 src/autobill/default_rules.yaml（两份必须一致）。",
        "# 先逐条核对：可以把店名改短或换成通用词，有歧义的词不要放。",
        "# 见 docs/notify.md#分类规则",
    ]
    grouped: dict[str, list[Export]] = defaultdict(list)
    for item in safe:
        grouped[item.category].append(item)
    for category in sorted(grouped):
        out += ["", f"{category}:"]
        for item in grouped[category]:
            note = " ".join(
                [x for x in (item.location, item.currency, item.confidence) if x] + remarks(item)
            )
            out.append(f"  - {json.dumps(item.name, ensure_ascii=False)}   # {note}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database", nargs="?", default="data/autobill.db", help="autobill.db")
    parser.add_argument("--yaml", action="store_true", help="Print a paste-ready YAML snippet.")
    parser.add_argument(
        "--review",
        action="store_true",
        help="Also list the names that could be a person's, for you to sort by hand.",
    )
    parser.add_argument(
        "--show-held",
        action="store_true",
        help="Also list the withheld names (server only: never paste these anywhere).",
    )
    args = parser.parse_args(argv)

    path = Path(args.database)
    if not path.exists():
        print(f"找不到数据库 {path}。写出它的完整路径，比如 ~/autobill-docker/data/autobill.db。")
        return 1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = rows_of(conn)
    except sqlite3.OperationalError as exc:
        print(f"读不出 ai_categories：{exc}。这个数据库可能还没用过 AI 分类。")
        return 1

    safe, review, held = split(rows)
    spellings = sum(item.variants for item in safe)
    unsure = len(rows) - spellings - len(review) - len(held)
    print(
        f"# 共 {len(rows)} 条：可公开 {len(safe)} 条（合并了 {spellings} 种写法），"
        f"要你自己判断 {len(review)} 条，保留在服务器 {len(held)} 条，"
        f"AI 没把握 {unsure} 条（不导出）"
    )
    print("# 粘之前扫一眼：像人名的不要放进仓库。")
    for line in as_yaml(safe) if args.yaml else as_table(safe):
        print(line)
    if review and not args.review:
        print(f"# 另外 {len(review)} 条可能是人名的要你自己判断，加 --review 看。")
    if args.review:
        print("")
        print("# 下面这些确认是店名再往仓库里放，是人名就留在服务器上：")
        for row, why in review:
            print(f"# {row['merchant']}  ->  {row['category']}   （{why}）")
    if args.show_held:
        print("")
        print("# 下面这些只在服务器上看，不要粘进仓库或聊天：")
        for row, why in held:
            print(f"# {row['merchant']}  <- {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
