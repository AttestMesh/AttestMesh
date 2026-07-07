# Fugu Router Hermes Provider

This bundled Hermes provider points agents at the AttestMesh `fugu-router`
OpenAI-compatible endpoint.

It makes routing sticky by passing the Hermes session id through both:

- `x-session-id`
- request body `metadata.session_id` and `metadata.fugu_session_id`

`fugu-router` uses those values to keep a live Hermes session on the same Sakana
subscription unless that subscription hits a limit and failover is required.
