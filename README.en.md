# AutoBill

[中文](README.md) · **English**

A small, self-hosted tool that turns Chinese credit-card statement e-mails into tidy monthly progress e-mails. Deploy it with one Docker command; your data stays on your own server and mailbox.

Banks send their e-statements to a dedicated mailbox. AutoBill reads it over IMAP on a schedule (read-only), parses each statement with **deterministic rules** (no AI touches the amounts), reconciles it item by item against the bank's own totals, converts foreign currencies to CNY, and e-mails a progress report per statement month: which cards have issued, what is owed, where the money went. The original statements stay in your mailbox.

> **Status:** `v0.2.0` is out and runs every 30 minutes in Docker on the author's own server: parsing and reconciliation for three banks, one e-mail per statement month once every card is in, alert e-mails, and AI classification for merchants the rules miss. Roadmap in [project.md](project.md#6-分期路线) (Chinese).

## Supported banks (first version)
| Bank | Format | Status |
|---|---|---|
| Agricultural Bank of China (ABC) | HTML e-mail | ✅ supported |
| China Construction Bank (CCB) | HTML e-mail | ✅ supported (spending rows await more samples) |
| Bank of China (BOC) | PDF attachment | ✅ supported (incl. combined multi-card statements) |

## What it does / doesn't do
- ✅ Statement-month progress e-mails, laid out for Apple Mail on iPhone (dark mode included): cards issued so far, total owed, categories and top merchants; every transaction folded away, one tap to open.
- ✅ Standard statements (optional, local command): `autobill statement` renders one uniform HTML and PDF per statement (needs Edge or Chrome).
- ✅ Itemised reconciliation against each statement's own totals.
- ✅ Multi-currency: original currencies kept, converted to CNY at the rate of the statement e-mail's date.
- ✅ Alerts: an unknown e-mail, a failed statement, a new card number or a failed mailbox login each send one alert, never repeated.
- ❌ No payment reminders (due dates are shown), no bank APIs; no AI for parsing or amounts (optionally, AI classifies merchants the rules miss).

## Deploy (Docker)
The server only needs Docker:

```bash
mkdir -p autobill/data && cd autobill
curl -fsSLO https://raw.githubusercontent.com/YangMingUNSW/autobill/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/YangMingUNSW/autobill/main/config.example.yaml -o data/config.yaml
curl -fsSL https://raw.githubusercontent.com/YangMingUNSW/autobill/main/autobill.env.example -o autobill.env
# fill in data/config.yaml and autobill.env (the mailbox password), then:
docker compose run --rm autobill check-mailbox
docker compose up -d
```

Images exist for amd64 and arm64 (servers, NAS boxes, Apple silicon Macs). Full steps, mailbox setup and everyday commands: [docs/deploy.md](docs/deploy.md) and [docs/setup.md](docs/setup.md) (Chinese).

## Try it (no mailbox needed)
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
