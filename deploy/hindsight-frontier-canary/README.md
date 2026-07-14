# Hindsight frontier canary

This stack compares `grok-4.5` and plain `fugu` in fresh Hindsight databases. It
routes both candidates through a local SSH tunnel to the mesh-only Fugu router.

The Grok proxy uses official xAI list prices and stops at $20. Plain Fugu is
subscription-backed and has no practical credit ceiling for this test; its proxy
still records synthetic $1/M input/output usage units. Both proxies persist
reservations and open their circuit after an ambiguous restart or any upstream
401/402. The earlier `fugu-ultra` evidence remains in the run directory and its
separate `hindsight_frontier_fugu` database.

Production Hindsight and its outboxes are not part of this Compose project.
Run `./network-allow.sh add` after Compose creates the private bridge, and
`./network-allow.sh remove` before tearing the stack down. The rule accepts only
the two proxy ports from the canary subnet to its host gateway.
