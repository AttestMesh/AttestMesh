"""Fugu Router provider profile for Hermes Agent."""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class FuguRouterProfile(ProviderProfile):
    """OpenAI-compatible fugu-router endpoint with Hermes session affinity."""

    def build_extra_body(
        self, *, session_id: str | None = None, **context: Any
    ) -> dict[str, Any]:
        if not session_id:
            return {}
        return {
            "metadata": {
                "session_id": session_id,
                "fugu_session_id": session_id,
            }
        }

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        session_id: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not session_id:
            return {}, {}
        return {}, {
            "extra_headers": {
                "x-session-id": session_id,
                "x-hermes-session-id": session_id,
            }
        }


fugu_router = FuguRouterProfile(
    name="fugu-router",
    aliases=("fugu", "fugu-router", "sakana-fugu-router"),
    api_mode="chat_completions",
    display_name="Fugu Router",
    description="AttestMesh Fugu subscription router with session affinity",
    env_vars=(
        "FUGU_ROUTER_API_KEY",
        "MODEL_API_KEY",
        "FUGU_ROUTER_BASE_URL",
        "MODEL_BASE_URL",
    ),
    base_url="http://127.0.0.1:18410/v1",
    auth_type="api_key",
    fallback_models=(
        "fugu-ultra",
        "fugu",
    ),
)

register_provider(fugu_router)
