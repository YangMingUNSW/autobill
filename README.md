# AutoBill

**English** · [中文](README.zh-CN.md)

A small, self-hosted pipeline that turns Chinese credit-card statement e-mails into a monthly spending summary.

Bank statements are auto-forwarded to a dedicated mailbox. AutoBill reads that mailbox over IMAP (read-only), parses each statement with **deterministic rules (no AI)**, reconciles it against the bank's own totals, stores it in SQLite, converts foreign currencies to CNY, and e-mails a summary report.

> **Status:** early development — milestone M0 (project skeleton). Nothing is usable yet. See the roadmap in [project.md](project.md#6-分期路线).

## Supported banks (first version)
| Bank | Format |
|---|---|
| Agricultural Bank of China (ABC) | HTML e-mail |
| China Construction Bank (CCB) | HTML e-mail |
| Bank of China (BOC) | PDF attachment |

## What it does / doesn't do
- ✅ Summaries and charts: total spend, by category, by card, trends, top merchants; shows the payment due date.
- ❌ No payment reminders, no per-transaction listing in reports, no bank APIs, no AI.

## Repository contents
| Path | What's inside |
|---|---|
| [project.md](project.md) | Overview, scope, decisions, architecture, risks (Chinese) |
| [docs/](docs/) | Design documents per module, bank format specs, setup guide (Chinese) |
| [tests/fixtures/](tests/fixtures/README.md) | Real statements from the author, **anonymised** (identity data removed) |
| [CLAUDE.md](CLAUDE.md) | Rules for AI coding assistants working on this repo |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

Documentation is written in Chinese; code and comments are in English.

## Development
Requires [uv](https://docs.astral.sh/uv/) and Git. See [docs/development.md](docs/development.md).

## License
[MIT](LICENSE) © Larry Row
