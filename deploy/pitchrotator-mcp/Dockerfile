# syntax=docker/dockerfile:1.7
FROM node:22-slim@sha256:8607a9064d4a571140998ae9e52a3b3fcf9cff361d04642d5971e6cd76d39e27 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run typecheck

FROM node:22-slim@sha256:8607a9064d4a571140998ae9e52a3b3fcf9cff361d04642d5971e6cd76d39e27

ARG SOURCE_COMMIT
ARG SOURCE_TREE
ARG OVERLAY_COMMIT
LABEL org.opencontainers.image.source="https://github.com/AlbionaHoti/Pitch-Rotator" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      io.attestmesh.pitchrotator.source-tree="${SOURCE_TREE}" \
      io.attestmesh.pitchrotator.overlay-commit="${OVERLAY_COMMIT}" \
      io.attestmesh.pitchrotator.provider="redpill" \
      io.attestmesh.pitchrotator.model="z-ai/glm-5.2"

ENV NODE_ENV=production
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY tsconfig.json ./
COPY src ./src

USER node
EXPOSE 8787
CMD ["npm", "run", "start"]
