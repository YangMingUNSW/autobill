# Contributing to AutoBill

Thanks for your interest. AutoBill is a personal project maintained in spare time, so replies can take a while, but issues and pull requests are welcome.

## Before you start
- **Bugs:** open an issue with the *Bug report* form and include the output of `autobill --version`.
- **New features or banks:** open an issue first, so the approach is agreed before any code is written. The project's scope and decisions are in [docs/project.md](docs/project.md) (Chinese).
- **Security problems:** do not open an issue; see [SECURITY.md](SECURITY.md).

## Privacy comes first
This repository is public, and it is about bank statements.

- Never commit, attach or paste a real statement, e-mail, card number, name, address, phone number or e-mail address, not in code, an issue or a log.
- Sample statements live in `tests/fixtures/` and are anonymised by hand, following the checklist in [docs/security.md](docs/security.md#以后加入新样本时的检查清单) (Chinese). Spending, amounts and merchants may stay; identity data may not.
- CI scans every pull request for secrets ([gitleaks](https://github.com/gitleaks/gitleaks)) and identity data (`scripts/check_identity.py`).

## Development setup
Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pre-commit install    # ruff, pytest, the identity scan and gitleaks before each commit
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Tests never go online (exchange rates, IMAP and SMTP are faked) and always use a temporary `AUTOBILL_DATA_DIR`, never your real data.

## Pull requests
- One change per pull request, on its own branch (`feat/…`, `fix/…`, `docs/…`). CI must pass.
- When behaviour changes, update the documentation in the same pull request and add a line under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
- When an e-mail's look changes, regenerate the README's pictures in the same pull request (they are made from invented data):

  ```bash
  uv run --with playwright --with pillow python scripts/demo_screenshots.py
  ```

- Code, comments and commit messages are in English; the design documents in [docs/](docs/README.md) are in Simplified Chinese.

Where the code for each step lives is in [docs/architecture.md](docs/architecture.md); the full workflow, testing conventions and release steps are in [docs/development.md](docs/development.md) (both Chinese).

## Code of Conduct
Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## License
By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
