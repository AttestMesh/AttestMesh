"""Compatibility shim for older config references.

The credit ledger supersedes the original Langfuse-only orchestration callback.
"""

from fugu_credit import fugu_credit_handler as fugu_telemetry_handler
