# Security Policy

## Supported versions
AutoBill is released from `main`. Only the latest release receives security fixes.

| Version | Supported |
|---|---|
| 0.2.x (latest) | ✅ |
| < 0.2 | ❌ |

## Reporting a vulnerability
Please **do not** open a public issue for a security problem.

Report it privately through GitHub instead: go to the repository's **Security** tab and choose **Report a vulnerability**, or open [a new draft advisory](https://github.com/YangMingUNSW/autobill/security/advisories/new) directly. Include:

- what the issue is and where it is (file, command or configuration);
- steps to reproduce, or a proof of concept;
- the impact you expect (for example, credential exposure or data leaving the server).

This is a personal project maintained in spare time, so reports are handled on a best-effort basis. You will get an answer in the advisory thread, and a fix will be credited to you unless you prefer otherwise.

**Never attach real bank statements, e-mails, card numbers or passwords to a report.** Use the anonymised samples in [`tests/fixtures/`](tests/fixtures/README.md) or invented data.

## Security model
What AutoBill is designed to guarantee, so you can judge whether something is a vulnerability:

- **Read-only mailbox access.** Folders are opened with `EXAMINE` and messages fetched with `BODY.PEEK[]`: nothing is deleted, moved or marked as read.
- **Secrets stay out of files.** Mailbox passwords and the AI API key are read from environment variables (`AUTOBILL_IMAP_PASSWORD`, `AUTOBILL_SMTP_PASSWORD`, `AUTOBILL_AI_API_KEY`), never from `config.yaml`, and are never printed or logged.
- **Data stays on your server.** Statements and the SQLite database live in the data directory. The only outbound connections are:
  - your own IMAP and SMTP servers;
  - the [Frankfurter](https://frankfurter.dev) exchange-rate API, which receives a currency code and a date only;
  - optionally, the AI endpoint you configure, which receives merchant name, location and currency only: never amounts, dates or card numbers.
- **No AI in parsing or amounts.** Statements are parsed and reconciled with deterministic rules.
- **Unprivileged container.** The Docker image runs as a non-root user.
- **No identity data in the repository.** CI runs [gitleaks](https://github.com/gitleaks/gitleaks) and an identity-data scan (`scripts/check_identity.py`) on every pull request and every push to `main`; sample statements are anonymised.

The detailed design is in [docs/security.md](docs/security.md) (Simplified Chinese).
