# Stateless read-through mesh-state API (public tier). Run via tsx so it works on any
# Node build (native TS type-stripping is flag-gated on 22.x / can be compiled out).
FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev --no-fund --no-audit
COPY tsconfig.json ./
COPY src ./src

ENV LISTEN_ADDR=0.0.0.0:8787
EXPOSE 8787

# RPC_URL supplied at deploy time (sealed env). All other config defaults to the live C3 cluster.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8787/healthz >/dev/null 2>&1 || exit 1

CMD ["node_modules/.bin/tsx", "src/server.ts"]
