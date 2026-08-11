# Contributing

Thank you for helping improve `quant_trading`. This repository is a self-hosted quantitative-trading system, so contributions must preserve its execution, risk, data, and audit boundaries.

## Before you start

- Read [AGENTS.md](AGENTS.md), [docs/README.md](docs/README.md), and the relevant domain contract before changing code.
- Open or review an issue for larger changes; keep each pull request focused on one problem.
- Never commit broker credentials, API keys, JWT secrets, `.env` files, runtime data, logs, database files, private account data, or model artifacts.
- Use paper, backtest, fixture, or isolated test environments. Do not test changes against a live broker or real account.

## Change expectations

- Describe the canonical authority, writer, and any path being replaced or removed.
- Preserve one production calculator and one production writer for each fact.
- Keep research, ML, LLM, and agent-generated changes advisory, shadow, or behind the existing review and release gates unless the project contracts explicitly allow otherwise.
- Do not bypass `RiskPolicyService`, `RiskGovernor`, Safety, V16, Coordinator, Canary, or effect evidence.
- Avoid adding a parallel service, table, worker, scheduler, threshold, compatibility field, or wrapper unless the change also removes a real duplicate or is required by an existing contract.
- Update the relevant documentation and tests with the implementation.

## Validation

Run the smallest reliable checks first, then expand them when the change affects a broader boundary:

```bash
git diff --check
.venv/bin/python -m py_compile <changed-python-files>
.venv/bin/pytest <focused-tests>
cd web_frontend && npm run typecheck && npm run build
```

For database, API, execution, or runtime changes, follow the applicable procedures in [docs/README.md](docs/README.md). Include the commands and results in the pull request description.

## Pull requests

Please include:

- the problem and intended behavior;
- the affected authority and call chain;
- migrations, compatibility changes, and rollback considerations;
- targeted test results and any known limitations;
- confirmation that no credentials or production data are included.

Avoid unrelated formatting or generated runtime files. Maintainers may request a narrower patch when a change expands authority or leaves the replaced path in place.
