# Narrow runtime overlay: preserve the exact deployed v43 application and
# replace only node-postgres pool lifecycle handling.
FROM ghcr.io/dmvt/synclave-app:v43@sha256:f382da88f4194316b330ccb447996d8569ded5655016566fd5d2e923eddbe853

COPY pool.ts /app/services/api/src/db/pool.ts
