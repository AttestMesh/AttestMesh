FROM ghcr.io/vectorize-io/hindsight:0.8.4

USER root
COPY --chown=1000:1000 budget_proxy.py /opt/attestmesh/budget_proxy.py
COPY --chown=1000:1000 reconcile_ambiguous.py /opt/attestmesh/reconcile_ambiguous.py
COPY --chown=1000:1000 reset_provider_auth.py /opt/attestmesh/reset_provider_auth.py
USER 1000:1000

ENTRYPOINT ["/app/api/.venv/bin/python", "/opt/attestmesh/budget_proxy.py"]
