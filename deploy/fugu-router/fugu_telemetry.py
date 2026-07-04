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
    """Pull the orchestration/visible token split out of the raw usage block.

    Observed Fugu shape (live, 2026-07-04): the orchestration counts are NESTED inside
    the standard OpenAI detail blocks —
      usage.prompt_tokens_details.orchestration_input_tokens / orchestration_input_cached_tokens
      usage.completion_tokens_details.orchestration_output_tokens
    so those blocks must be mined explicitly, not skipped as "known" keys.
    """
    usage = _as_dict(_as_dict(response_obj).get("usage", {}))
    fugu = {}
    # 1. Explicit: any orchestration_*/cached counter inside the details blocks.
    for block in ("prompt_tokens_details", "completion_tokens_details"):
        for ik, iv in _as_dict(usage.get(block, {})).items():
            if isinstance(iv, (int, float)) and ("orchestration" in ik or "cached" in ik):
                fugu[ik] = iv
    # 2. Defensive sweep: any non-standard top-level usage keys (future schema drift).
    for k, v in usage.items():
        if k in _KNOWN_USAGE_KEYS or v in (None, {}, []):
            continue
        if isinstance(v, dict):
            fugu.update({f"{k}.{ik}": iv for ik, iv in v.items()})
        else:
            fugu[k] = v
    orch = sum(v for k, v in fugu.items()
               if "orchestration" in k and "cached" not in k and isinstance(v, (int, float)))
    total = usage.get("total_tokens")
    if orch and isinstance(total, (int, float)) and total > 0:
        fugu["orchestration_tokens_total"] = orch
        fugu["orchestration_ratio"] = round(orch / total, 4)
        fugu["visible_tokens"] = total - orch
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
