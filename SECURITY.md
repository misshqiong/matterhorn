# Security policy

## Supported versions

Security fixes are provided for the latest minor release.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use the repository
host's private security-advisory flow and include affected version, deployment
mode, reproduction steps, impact, and any proposed mitigation. Maintainers
should acknowledge a complete report within seven days.

## Deployment boundary

Matterhorn v1 intentionally does not implement authentication, authorization,
or multi-tenant policy. Put REST and MCP deployments behind the host's trusted
authentication and network boundary. Keep database and LLM credentials in
environment variables or a secret manager, never in cards or source excerpts.

PostgreSQL must target a writable primary. Do not use a replica DSN or a proxy
that can route reads to replicas. Back up append-only assertions and test
replay before relying on projection backups.

Source excerpts may contain sensitive conversation content. Apply retention,
redaction, encryption, and access controls appropriate to the originating
system before ingestion.
