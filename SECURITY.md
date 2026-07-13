# Security Policy

## Reporting

If you discover a security issue in Multi-Agency, please open a private GitHub security advisory on this repository or email the maintainer via GitHub profile contact. Do not file a public issue for vulnerabilities that could expose user machines or credentials.

## Scope

Multi-Agency runs shell commands, opens cmux panes, and starts `pi` with elevated tool access. Treat installed packages as **full system access** (same as any pi extension).

## Hardening tips

- Only install from sources you trust (`pi install git:…` / npm).
- Keep project `.pi/agency/inbox` and `artifacts` out of git (see `.gitignore`).
- Do not put API keys in delegate payloads or boot prompts; use environment / secret managers outside the bus.
- Prefer Orchestrator mediation; do not widen peer ACL until you understand the trust model.

## Secrets in this repo

This public repository must not contain `.env` files, tokens, private keys, or live agency inboxes. Runtime state is gitignored by default.
