# Recovery compatibility overlay: inherit the already-reviewed immutable image
# and replace only the startup validator.  This prevents a mutable upstream tag
# or unrelated helper source from entering the incident-remediation roll.
FROM ghcr.io/attestmesh/hindsight-budget-proxy@sha256:cc9eb81b37880c7faaaa87fc5741766014cdd44f4c82154269767cdc257617d2

USER root
COPY --chown=1000:1000 budget_proxy.py /opt/attestmesh/budget_proxy.py
USER 1000:1000
