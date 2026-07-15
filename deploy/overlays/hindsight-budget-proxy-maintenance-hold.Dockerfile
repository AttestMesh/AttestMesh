# Narrow runtime overlay: preserve the exact deployed budget-proxy image and
# add only the reviewed continuous-run budget state/API helpers.
FROM ghcr.io/attestmesh/hindsight-budget-proxy@sha256:639e93cf700448750dbc6bebc957f62bd4f9e22c62e7ec6f55a25c7d6801000e

COPY --chown=1000:1000 deploy/hindsight-budget-proxy/budget_proxy.py /opt/attestmesh/budget_proxy.py
COPY --chown=1000:1000 deploy/hindsight-budget-proxy/reconcile_ambiguous.py /opt/attestmesh/reconcile_ambiguous.py
COPY --chown=1000:1000 deploy/hindsight-budget-proxy/reset_provider_auth.py /opt/attestmesh/reset_provider_auth.py
COPY --chown=1000:1000 deploy/hindsight-budget-proxy/control_marker.py /opt/attestmesh/control_marker.py
