# Contributing

Cognitive Core is alpha — pull requests, bug reports and "this is weird" issues are all
welcome.

## Quick start for hacking

```bash
git clone https://github.com/mocartlex-wq/cognitive-core
cd launch
make init
# point IMAGE_API at a local build:
echo 'IMAGE_API=cognitive-core-api:dev' >> .env
make build && make up
```

## Repo layout

```
launch/
├── docker-compose.public.yml   # one-file production-grade compose
├── .env.example                # template — do NOT commit your .env
├── Makefile                    # convenience targets
├── quickstart.sh               # curl-pipe installer
├── nats/nats.conf              # NATS WS + JetStream config
├── scripts/smoke-test.sh       # E2E test — also runs in CI
├── docs/
│   ├── HARDENING.md            # production checklist
│   ├── ROOMS.md                # rooms API reference
│   ├── MEMORY.md               # 5-layer model + GDPR
│   ├── MCP.md                  # Claude Code integration
│   └── UPGRADING.md            # migration playbook
├── extras/                     # rooms.py, pg-to-nats.py mounted into containers
└── README.md
```

The actual API source code lives in a separate repo:
[`cognitive-core/api`](https://github.com/cognitive-core/api). This `launch` repo is
the deployment artefact.

## Pull requests

- One change per PR.
- Run `make smoke` locally — CI runs it on every push.
- Update `docs/` if you change behaviour users see.
- Conventional commits welcomed (`feat:`, `fix:`, `docs:`, `chore:`) but not
  required.

## Reporting bugs

Open an issue with:
1. `make versions` output
2. Steps to reproduce
3. `make logs` excerpt around the failure (redact secrets)
4. Expected vs actual

## Security

Send security reports privately to **security@cognitive-core.dev**. We aim for an
ack within 72 hours. See `SECURITY.md` for the disclosure policy.

## Code style

- Python: `ruff` defaults, 100-char line, type hints on public functions.
- Bash: `set -euo pipefail`, ShellCheck clean.
- YAML: 2-space indent, no trailing spaces.
- Docs: Markdown, soft-wrap at 90 chars where convenient.

## Communication

- Bugs / features: GitHub Issues
- Chat: `#cognitive-core` on the Anthropic Discord (link in README)
- Security: email above
