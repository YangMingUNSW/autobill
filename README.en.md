# AutoBill

[中文](README.md) · **English**

A small, self-hosted tool that turns Chinese credit-card statement e-mails into a monthly spending summary. Your data stays on your own computer.

Bank statements are auto-forwarded to a dedicated mailbox. AutoBill reads that mailbox over IMAP (read-only), parses each statement with **deterministic rules (no AI)**, reconciles it item by item against the bank's own totals, stores it in SQLite, converts foreign currencies to CNY, and e-mails a summary report.

> **Status:** first version in development. Parsing, reconciliation, local import and a terminal report with categories, trend and top merchants work for all three banks (ABC, CCB, BOC); e-mailed reports and automatic fetching are in progress. See the roadmap in [project.md](project.md#6-分期路线) (Chinese).

## Supported banks (first version)
| Bank | Format | Status |
|---|---|---|
| Agricultural Bank of China (ABC) | HTML e-mail | ✅ supported |
| China Construction Bank (CCB) | HTML e-mail | ✅ supported (spending rows await more samples) |
| Bank of China (BOC) | PDF attachment | ✅ supported (incl. combined multi-card statements) |

## What it does / doesn't do
- ✅ Summaries and charts: total spend, by category, by card, trends, top merchants; shows the payment due date.
- ✅ Itemised reconciliation against each statement's own totals.
- ✅ Multi-currency: original currencies kept, converted to CNY at the rate of the statement e-mail's date.
- ❌ No payment reminders, no per-transaction listing in reports, no bank APIs, no AI.

## Try it (development version)
Requires [uv](https://docs.astral.sh/uv/) and Git. Uses the anonymised sample statements in the repo; no mailbox needed:

```powershell
git clone https://github.com/YangMingUNSW/autobill.git
cd autobill
$env:AUTOBILL_DATA_DIR = "$env:TEMP\autobill-dev"   # a scratch data directory
uv run autobill import-dir tests/fixtures
uv run autobill report --month 2026-08
```

## Repository contents
| Path | What's inside |
|---|---|
| [project.md](project.md) | Overview, scope, decisions, architecture, risks |
| [docs/](docs/) | Module designs, bank format specs, development process, setup guide |
| [tests/fixtures/](tests/fixtures/README.md) | Real statements from the author, **anonymised** (identity data removed) |
| [CLAUDE.md](CLAUDE.md) | Rules for AI coding assistants working on this repo |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

Documentation is written in Chinese; code and comments are in English.

## Development
See [docs/development.md](docs/development.md) (Chinese).

## License
[MIT](LICENSE) © Larry Row
