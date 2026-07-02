# Hermes workbench image for the AttestMesh ssh node.
#
# One image, four compose services (deploy/compose/ssh-node.yaml):
#   sshd / sshd-mesh   — the ubuntu workbench shells (:1022 bridge, :1023 mesh)
#   hermes-gateway     — supervised `hermes gateway` (Matrix connector et al.)
#   paseo              — `paseo daemon start --foreground` (drives `hermes acp`)
#
# Bakes the Nous Research Hermes Agent (editable install, uv-managed CPython
# 3.11 — the exact layout proven on the live node) plus Node 22 + the Paseo
# CLI, so the agent runtime lives in the image instead of the container's
# ephemeral layer. All agent STATE stays under /root (the compose `workspace`
# volume): ~/.hermes (config, .env secrets, crypto store), ~/.paseo,
# ~/dstack-deep-dive. Nothing secret is baked here.
ARG HERMES_REF=b3bc302370aadeb792ceb0a899cf6a575ed130bb
ARG PASEO_VERSION=0.1.103

# Node 22 LTS donor (same pin as upstream hermes-agent Dockerfile).
FROM node:22-bookworm-slim@sha256:7af03b14a13c8cdd38e45058fd957bf00a72bbe17feac43b1c15a689c029c732 AS node_source

FROM ubuntu:26.04@sha256:53958ec7b67c2c9355df922dd08dbf0360611f8c3cdb656875e81873db9ffdba
ARG HERMES_REF
ARG PASEO_VERSION

ENV PYTHONUNBUFFERED=1 \
    UV_NO_CONFIG=1 \
    UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

# Workbench toolset (superset of the old dockerfile_inline shells) + the extras
# the live node accumulated by hand: ffmpeg (stt/tts), sudo, xvfb + fonts
# (browser/computer-use tools). Playwright system deps come later via
# `playwright install-deps`.
RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates openssh-server openssh-client \
    build-essential git python3 python3-pip python3-venv libffi-dev \
    curl wget jq ripgrep unzip rsync \
    iproute2 iputils-ping dnsutils netcat-openbsd socat wireguard-tools \
    vim nano tmux htop less \
    ffmpeg sudo xvfb \
  && rm -rf /var/lib/apt/lists/*

# sshd: key-only root login. Port is chosen per-service at runtime
# (`-o Port=…`); host keys are generated at first boot onto the /root volume
# so they are stable across rolls and never shipped in the image.
RUN mkdir -p /run/sshd \
  && sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config \
  && sed -i 's/^#\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
  && sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config \
  && sed -i 's/^#\?KbdInteractiveAuthentication .*/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config \
  && sed -i 's/^#\?PermitEmptyPasswords .*/PermitEmptyPasswords no/' /etc/ssh/sshd_config \
  && sed -i 's@session\s*required\s*pam_loginuid.so@session optional pam_loginuid.so@g' /etc/pam.d/sshd \
  && rm -f /etc/ssh/ssh_host_*

# Node 22 + npm (donor copy, same approach as upstream) + Paseo CLI.
COPY --from=node_source /usr/local/bin/node /usr/local/bin/node
COPY --from=node_source /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
  && npm install -g "@getpaseo/cli@${PASEO_VERSION}" \
  && npm cache clean --force

# uv (pinned) + CPython 3.11, mirroring setup-hermes.sh's desktop path.
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /usr/local/bin/
RUN uv python install 3.11

# Hermes Agent, editable install at the same path as the hand-built node.
RUN git clone https://github.com/NousResearch/hermes-agent.git /usr/local/lib/hermes-agent \
  && cd /usr/local/lib/hermes-agent \
  && git checkout "${HERMES_REF}" \
  && uv venv --python 3.11 venv \
  && (uv pip install -p venv/bin/python -e ".[all]" \
      || uv pip install -p venv/bin/python -e ".") \
  && ln -sf /usr/local/lib/hermes-agent/venv/bin/hermes /usr/local/bin/hermes \
  && echo git > .install_method

# Playwright chromium baked into the image (PLAYWRIGHT_BROWSERS_PATH is
# outside /root so the volume mount doesn't shadow it). Tolerated to fail if
# the resolved hermes extras don't pull playwright.
RUN if [ -x /usr/local/lib/hermes-agent/venv/bin/playwright ]; then \
      /usr/local/lib/hermes-agent/venv/bin/playwright install-deps chromium \
      && /usr/local/lib/hermes-agent/venv/bin/playwright install chromium \
      && rm -rf /var/lib/apt/lists/*; \
    fi
