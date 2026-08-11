# Security Policy

This project handles broker authentication, application sessions, external network requests, model artifacts, subprocess jobs, and risk-control decisions. Please treat security reports as sensitive.

## Supported code

The `main` branch is the actively maintained branch. There are currently no published release versions.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting or Security Advisories for this repository when available. If private reporting is unavailable, contact the maintainer through a private GitHub channel before disclosing details publicly. Do not open a public issue containing credentials, tokens, private data, or a working exploit.

Include, when safe:

- the affected commit, file, endpoint, or configuration;
- a concise description of the trust boundary and impact;
- reproducible steps using synthetic data;
- sanitized logs or a minimal proof of concept;
- any suggested mitigation.

Never send real broker credentials, API keys, account identifiers, or production database contents.

## In-scope reports

Please report issues involving:

- credential, token, session, OAuth, or authentication exposure;
- unauthorized broker actions, risk-gate bypass, or privilege escalation;
- SSRF, unapproved network egress, webhook abuse, or path traversal;
- arbitrary file writes, unsafe subprocess or GitHub Actions behavior;
- unsafe pickle/joblib/model-artifact loading or dependency supply-chain attacks;
- prompt injection that can affect governance, policy, or execution decisions;
- sensitive data exposure through logs, audit records, API responses, or error messages.

## Safe testing boundaries

- Do not test against a live broker, real account, production service, or another user's data.
- Use an isolated environment, synthetic credentials, disposable databases, and non-production endpoints.
- Stop testing immediately if a real order, position, credential, or private record could be affected.

We will acknowledge reports as soon as practical, assess severity and affected versions, coordinate remediation with the reporter when possible, and disclose fixes after the risk is understood and a patch is available.
