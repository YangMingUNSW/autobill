"""Export the AI's merchant categories so they can be turned into keyword rules.

The stored answers in `ai_categories` live only in the database on the server. Rules in
rules.example.yaml live in this public repository, so a merchant promoted to a rule is
backed up by Git and, being a rule, always wins over a stored answer
(see docs/notify.md#分类规则).

Read only: the database is opened with mode=ro, so this cannot change anything and is safe
to run while `serve` is going.

Privacy: `ai_categories.merchant` is the parser's merchant name, but falls back to the raw
description when the parser found no merchant (autobill/classify.py). A WeChat or Alipay
payment to a person prints that person's name in the description, and
tests/fixtures/README.md rules that other people's real names stay out of the repository.
So every row lands in one of three buckets:

  * held    - no transaction carries the name (so the name IS a raw description), or the
              name contains a payment-channel word. Never printed by default.
  * review  - two or three Chinese characters and nothing else. Could be a chain (古茗,
              海底捞) or could be a person; only you can tell. Never printed by default.
  * export  - everything else, printed for pasting into the repository.

The default output is therefore safe to paste as it stands. `--review` and `--show-held`
print the other two buckets FOR READING ON THE SERVER; never paste their output into the
repository or into a chat. Held rows keep working on the server either way: this script
only reads.

Usage on the server, in the folder holding compose.yaml:

    python3 scripts/export_ai_categories.py data/autobill.db            # review table
    python3 scripts/export_ai_categories.py data/autobill.db --yaml     # paste-ready YAML
    python3 scripts/export_ai_categories.py data/autobill.db --review   # the middle bucket

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
from pathlib import Path

# Payment channels that print a person's name instead of a shop: the description reads
# "财付通-<name>" or "支付宝-<name>". Matched anywhere in the name.
CHANNEL_WORDS = re.compile(r"财付通|支付宝|微信|转账|转帐|代付|收款|汇款|个人")

# CJK Unified Ideographs, compared by code point: a character class holding the two range
# ends would put a barely-renderable literal in the source, which tools like to mangle.
CJK_FIRST, CJK_LAST = 0x4E00, 0x9FFF

EXPORT, REVIEW, HOLD = "export", "review", "hold"


def is_bare_chinese_name(name: str) -> bool:
    """Two or three Chinese characters and nothing else - the shape of a personal name,
    but also of plenty of chains (古茗, 海底捞), so these are neither exported nor dropped."""
    return 2 <= len(name) <= 3 and all(CJK_FIRST <= ord(c) <= CJK_LAST for c in name)


def verdict_for(merchant: str, parsed_count: int) -> tuple[str, str]:
    """Which bucket this merchant belongs in, and why."""
    if parsed_count == 0:
        return HOLD, "原始描述（解析器没认出商户）"
    if CHANNEL_WORDS.search(merchant):
        return HOLD, "名字里有支付渠道字样"
    if is_bare_chinese_name(merchant):
        return REVIEW, "两三个汉字：可能是连锁店，也可能是人名"
    return EXPORT, ""


def rows_of(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.merchant, a.category, a.guess, a.confidence, a.location, a.currency,"
        " (SELECT COUNT(*) FROM transactions t WHERE t.merchant = a.merchant) AS parsed"
        " FROM ai_categories a"
        " ORDER BY a.category IS NULL, a.category, a.merchant"
    ).fetchall()


def split(
    rows: list[sqlite3.Row],
) -> tuple[list[sqlite3.Row], list[tuple[sqlite3.Row, str]], list[tuple[sqlite3.Row, str]]]:
    """(exportable, [(review row, why)], [(held row, why)]). Unsure answers are in none."""
    safe: list[sqlite3.Row] = []
    review: list[tuple[sqlite3.Row, str]] = []
    held: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        bucket, why = verdict_for(row["merchant"], row["parsed"])
        if bucket == HOLD:
            held.append((row, why))
        elif not row["category"]:
            continue  # the AI was not sure: not a classification
        elif bucket == REVIEW:
            review.append((row, why))
        else:
            safe.append(row)
    return safe, review, held


def as_table(safe: list[sqlite3.Row]) -> list[str]:
    out = ["# 分类 | 商户名 | 地点 | 币种 | 把握"]
    for r in safe:
        fields = (r["category"], r["merchant"], r["location"] or "", r["currency"] or "")
        out.append(" | ".join((*fields, r["confidence"])))
    return out


def as_yaml(safe: list[sqlite3.Row]) -> list[str]:
    """Grouped by category, ready to merge into rules.example.yaml by hand."""
    out = [
        "# 合并进 rules.example.yaml 和 src/autobill/default_rules.yaml（两份必须一致）。",
        "# 先逐条核对：可以把店名改短或换成通用词，有歧义的词不要放。",
        "# 见 docs/notify.md#分类规则",
    ]
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in safe:
        grouped[r["category"]].append(r)
    for category in sorted(grouped):
        out += ["", f"{category}:"]
        for r in grouped[category]:
            note = " ".join(x for x in (r["location"], r["currency"], r["confidence"]) if x)
            out.append(f"  - {json.dumps(r['merchant'], ensure_ascii=False)}   # {note}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database", nargs="?", default="data/autobill.db", help="autobill.db")
    parser.add_argument("--yaml", action="store_true", help="Print a paste-ready YAML snippet.")
    parser.add_argument(
        "--review",
        action="store_true",
        help="Also list the two-or-three-character Chinese names, for you to sort by hand.",
    )
    parser.add_argument(
        "--show-held",
        action="store_true",
        help="Also list the withheld names (server only: never paste these anywhere).",
    )
    args = parser.parse_args(argv)

    path = Path(args.database)
    if not path.exists():
        print(f"找不到数据库 {path}。在放 compose.yaml 的目录里运行，或者写出它的路径。")
        return 1
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = rows_of(conn)
    except sqlite3.OperationalError as exc:
        print(f"读不出 ai_categories：{exc}。这个数据库可能还没用过 AI 分类。")
        return 1

    safe, review, held = split(rows)
    unsure = len(rows) - len(safe) - len(review) - len(held)
    print(
        f"# 共 {len(rows)} 条：可公开 {len(safe)} 条，要你自己判断 {len(review)} 条，"
        f"保留在服务器 {len(held)} 条，AI 没把握 {unsure} 条（不导出）"
    )
    for line in as_yaml(safe) if args.yaml else as_table(safe):
        print(line)
    if review and not args.review:
        print(f"# 另外 {len(review)} 条两三个汉字的名字要你自己判断，加 --review 看。")
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
