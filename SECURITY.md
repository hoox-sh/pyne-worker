# Security Policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately to the
maintainers.

Include:

- the affected version or commit,
- a minimal reproduction,
- the impact (what an attacker can read, change, or execute),
- any suggested fix.

## Secrets

Never commit API keys, tokens, or credentials. Use environment variables.
OpenOrchestrate's `oo doctor` command scans generated files for common key
patterns.

## Gates

The team stops and asks before anything irreversible:

- Ask first before spending money or buying anything.
- Ask first before changing permissions or logins.
- Ask first before running database migrations or destructive commands.
- Ask first before deploying to production.
- Ask first before deleting data.
- Ask first before messaging people outside my team.

When rules conflict, Ask-first wins.
