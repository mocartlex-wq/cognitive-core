# Security policy

## Supported versions

Cognitive Core is alpha; only the latest minor version receives security patches.

| Version | Supported |
|---------|-----------|
| 0.5.x   | ✅ |
| < 0.5   | ❌ |

## Reporting a vulnerability

Email **security@cognitive-core.dev** with:
- A clear description of the issue
- Reproduction steps or PoC
- The version / commit you tested
- Your name / handle for credit (optional)

Please do **not** open public GitHub issues for security problems.

We aim to:
- Acknowledge within 72 hours
- Triage and assign severity within 7 days
- Ship a fix or mitigation within 30 days for high/critical findings

You'll be credited in the release notes unless you ask otherwise.

## Out of scope

- Issues that require physical access to the host
- Self-exploits as a legitimate authenticated user (we trust agent keys)
- Findings on `:latest` Docker image when a newer release exists
- Denial-of-service via resource exhaustion when no rate limit is configured (this
  is documented in `docs/HARDENING.md`)

## Hall of fame

- (your name here)
