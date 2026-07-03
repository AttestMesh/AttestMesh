"""Fugu orchestration-token telemetry callback (fugu-subscription-pooling.md §4.2).

Fugu's responses separate visible-model tokens from internal orchestration tokens in
usage extras; no off-the-shelf tool reads them, and orchestration is the hidden
multiplier on allowance burn. This CustomLogger extracts whatever orchestration split
the response carries (schema not yet published — capture defensively) and attaches it,
plus {account, agent_id, agent_role, session_id}, as Langfuse trace metadata.

Registered in config.yaml: callbacks: ["langfuse", "fugu_telemetry.fugu_telemetry_handler"].
MUST never raise — a telemetry bug must not fail a completion.
"""

from litellm.integrations.custom_logger import CustomLogger

# usage keys LiteLLM/OpenAI already account for; anything else is Fugu-specific.
_KNOWN_USAGE_KEYS = {
    "prompt_tokens", "completion_tokens", "total_tokens",
    "prompt_tokens_details", "completion_tokens_details",
    "cache_creation_input_tokens", "cache_read_input_tokens",
}
_AGENT_HEADERS = {"x-agent-id": "agent_id", "x-agent-role": "agent_role", "x-session-id": "session_id"}


def _as_dict(obj):
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return {}


def _extract_fugu_usage(response_obj):
    """Pull the orchestration/visible token split out of the raw usage block."""
    usage = _as_dict(_as_dict(response_obj).get("usage", {}))
    extras = {k: v for k, v in usage.items() if k not in _KNOWN_USAGE_KEYS and v not in (None, {}, [])}
    # Fold nested containers (e.g. "token_details": {"orchestration_tokens": ...}) up a level.
    fugu = {}
    for k, v in extras.items():
        if isinstance(v, dict):
            fugu.update({f"{k}.{ik}": iv for ik, iv in v.items()})
        else:
            fugu[k] = v
    orch = next((v for k, v in fugu.items() if "orchestration" in k and isinstance(v, (int, float))), None)
    total = usage.get("total_tokens")
    if orch is not None and isinstance(total, (int, float)) and total > 0:
        fugu["orchestration_ratio"] = round(orch / total, 4)
    return fugu


class FuguTelemetry(CustomLogger):
    def _annotate(self, kwargs, response_obj):
        try:
            meta = kwargs.setdefault("litellm_params", {}).setdefault("metadata", {}) or {}
            kwargs["litellm_params"]["metadata"] = meta

            fugu = _extract_fugu_usage(response_obj)
            if fugu:
                meta["fugu_orchestration"] = fugu

            # account = which deployment/key served this (per-account burn views, §4.1)
            lp = kwargs.get("litellm_params") or {}
            account = (_as_dict(lp.get("model_info")).get("id")
                       or (kwargs.get("standard_logging_object") or {}).get("model_id"))
            if account:
                meta["account"] = account

            headers = ((kwargs.get("proxy_server_request") or {}).get("headers") or {})
            for hdr, key in _AGENT_HEADERS.items():
                val = headers.get(hdr) or headers.get(hdr.replace("-", "_"))
                if val:
                    meta[key] = val
        except Exception as e:  # never fail the request on telemetry
            print(f"fugu_telemetry: swallow {type(e).__name__}: {e}", flush=True)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._annotate(kwargs, response_obj)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._annotate(kwargs, response_obj)


fugu_telemetry_handler = FuguTelemetry()
