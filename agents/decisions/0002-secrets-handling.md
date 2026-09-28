# 0002. Secrets handling

- **Status:** accepted
- **Date (UTC):** project start
- **Deciders:** project owner

## Context
The project uses a Hugging Face token and may later use other credentials. Leaked credentials in git history are hard to remediate.

## Decision
- Secrets are supplied only through environment variables (`HF_KEY`) or a secret manager. `.env` is gitignored; `.env.example` contains empty placeholders.
- Agents never open `.env` (denied in `.claude/settings.json`), and never print, log, or write credentials to files, docs, fixtures, or commits.
- Enforcement: `scripts/check_repo_hygiene.py` (pattern scan; pre-commit + CI) and gitleaks (pre-commit).
- Code reading `HF_KEY` must map it explicitly to whatever variable a library expects (e.g., `HF_TOKEN`) at runtime, in memory only.
- A credential that appears in any chat, log, or commit is considered compromised and must be rotated.

## Consequences
Tests needing credentials skip when the variable is absent. CI uses repository secrets, never committed values.
