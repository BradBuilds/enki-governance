# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public issue.

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**), or open a private security advisory.

Include: a description, steps to reproduce, affected version/commit, and impact. We aim to
acknowledge within a few business days and will coordinate a fix and disclosure timeline
with you.

## Scope

Enki Governance runs **fully offline** by default (no external calls, no telemetry). The most
relevant areas: document ingestion/OCR, the API surface, authentication/profiles, and the
governed collapse/verification path. Deployment hardening (network exposure, the Postgres and
Ollama services, the emulated root key) is the operator's responsibility — see SETUP.md.
