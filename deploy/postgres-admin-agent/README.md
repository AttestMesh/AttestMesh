Postgres Admin Agent
====================

Small Matrix bot for administering the standalone PostgreSQL node.

It logs into an existing Matrix homeserver, joins one configured room, and answers
allowlisted operators only when explicitly tagged, for example
`@pg-admin-agent !pg status`. It supports deterministic commands:

- `!pg status`
- `!pg metrics`
- `!pg query <read-only SQL>`
- `!pg exec <SQL>` followed by `confirm <token>`

Natural-language messages are routed through an OpenAI-compatible LLM with the same
database tools plus a bounded Prometheus query tool when `PROMETHEUS_URL` is set.
Write/admin SQL is staged and requires an explicit confirmation token.
